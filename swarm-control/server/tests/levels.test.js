import test from 'node:test';
import assert from 'node:assert/strict';
import {deviceLevels,compareNetworkRows,networkLinkLabel} from '../view/levels-model.js';
const now=Date.now(),host={host:'laptop.local',observed_hostname:'laptop',addresses:['10.42.0.2'],status:'live',sampled_at:now/1000,network_mbps:3,ram_percent:54,disk_busy_percent:82};
const data={devices:[{ip:'10.42.0.2',mac:'aa'},{ip:'10.42.0.3',mac:'bb'}],link_traffic:{updated:new Date(now).toISOString(),peers:{aa:{rx_mbps:2,tx_mbps:2},bb:{rx_mbps:1,tx_mbps:2}}}};
test('SSH address identity deduplicates and passive peers cover other devices',()=>{const rows=deviceLevels(data,{hosts:[host]},'network',now);assert.equal(rows.length,1);assert.equal(rows[0].value,3);assert.equal(rows[0].name,'10.42.0.3 · gateway only');});
test('unmeasured RAM is unknown and stale hosts lose measurements',()=>{assert.deepEqual(deviceLevels(data,{hosts:[host]},'ram',now).map(r=>r.value),[54,null]);assert.equal(deviceLevels(data,{hosts:[host]},'disk',now+16000)[0].value,null);});
test('unnamed device cannot match unavailable host with no observed hostname',()=>{const rows=deviceLevels(data,{hosts:[{host:'other.local',status:'unavailable'}]},'ram',now);assert.equal(rows.length,3);});

test('GPU charts use measured counters and suppress stale or absent values',()=>{
 const metrics={hosts:[{...host,hardware:{gpus:[{name:'RTX',utilization_percent:60,memory_total_mib:100,memory_free_mib:25},{name:'Intel'}]}}]};
 assert.deepEqual(deviceLevels({},metrics,'gpu',now).map(r=>r.value),[60,null]);
 assert.deepEqual(deviceLevels({},metrics,'vram',now).map(r=>r.value),[75,null]);
 assert.equal(deviceLevels({},metrics,'gpu',now+16000)[0].value,null);
});

test('CPU chart explains measurement failures and supplies interval provenance',()=>{
 const h={...host,percent:null,status:'permission_denied',sample_age_seconds:1};
 const rows=deviceLevels({}, {hosts:[h]},'cpu',now);
 assert.equal(rows[0].value,null);assert.equal(rows[0].unavailableLabel,'Permission denied');
 assert.match(rows[0].basis,/Permission denied/);
 const live=deviceLevels({}, {hosts:[{...h,status:'live',percent:25,window_seconds:5}]},'cpu',now)[0];
 assert.equal(live.value,25);assert.match(live.basis,/5.0s average/);
});

test('each machine exposes distinct interface and directional series without a duplicate host total',()=>{
 const stamp=Date.now();
 const h={host:'worker.local',status:'live',sampled_at:stamp/1000,addresses:['10.42.0.2'],network_mbps:99,network_interfaces:[
  {name:'eth0',ifindex:2,state:'up',status:'live',sampled_at:stamp/1000,rx_mbps:4,tx_mbps:2,network_mbps:6},
  {name:'wlan0',ifindex:3,state:'up',status:'live',sampled_at:stamp/1000,rx_mbps:1,tx_mbps:3,network_mbps:4},
 ]};
 const network={devices:[{ip:'10.42.0.2',hostname:'worker.local',mac:'aa'}]};
 const rows=deviceLevels(network,{hosts:[h]},'network',stamp);
 assert.equal(rows.length,2);assert.deepEqual(rows.map(row=>row.value),[6,4]);
 assert.deepEqual(rows.map(row=>row.name),['worker.local · eth0','worker.local · wlan0']);
 assert.notEqual(rows[0].id,rows[1].id);
 assert.deepEqual(deviceLevels(network,{hosts:[h]},'send',stamp).map(row=>row.value),[2,3]);
 assert.deepEqual(deviceLevels(network,{hosts:[h]},'receive',stamp).map(row=>row.value),[4,1]);
 assert.deepEqual(deviceLevels(network,{hosts:[h]},'network',stamp+16000).map(row=>row.value),[null,null]);
});


test('network rows sort by highest throughput including gateway observations',()=>{
 const stamp=Date.now();
 const host={host:'worker.local',network_interfaces:[{name:'eth0',ifindex:2,status:'live',sampled_at:stamp/1000,network_mbps:1}]};
 const data={devices:[{ip:'10.42.0.3',mac:'aa'}],link_traffic:{updated:new Date(stamp).toISOString(),peers:{aa:{rx_mbps:50,tx_mbps:50}}}};
 const rows=deviceLevels(data,{hosts:[host]},'network',stamp).sort(compareNetworkRows);
 assert.equal(rows[0].name,'10.42.0.3 · gateway only');
 assert.equal(rows[1].name,'worker.local · eth0');
 assert(!rows.some(row=>row.name.includes('interface unknown')));
});


test('cached mDNS labels disclose their source without changing measured network values',()=>{
 const stamp=Date.now();
 const data={devices:[{ip:'10.42.0.3',mac:'aa',hostname:'device.local',hostname_source:'cached_mdns'}],link_traffic:{updated:new Date(stamp).toISOString(),peers:{aa:{rx_mbps:3,tx_mbps:2}}}};
 const row=deviceLevels(data,{hosts:[]},'network',stamp)[0];
 assert.equal(row.name,'device.local · gateway only');
 assert.equal(row.value,5);assert.match(row.basis,/last mDNS observation/);
});

test('disconnected interfaces do not report zero traffic and stale state remains unknown',()=>{
 const h={...host,network_interfaces:[{name:'eth0',ifindex:2,state:'down',status:'live',sampled_at:now/1000,network_mbps:0}]};
 const row=deviceLevels({}, {hosts:[h]},'network',now)[0];
 assert.equal(row.value,null);assert.equal(row.unavailableLabel,'Not connected');
 assert.equal(deviceLevels({}, {hosts:[h]},'network',now+16000)[0].unavailableLabel,'No data');
 assert.deepEqual(deviceLevels({}, {hosts:[host]},'network',now),[]);
});

test('negotiated link speed is separate from throughput and hides stale capacity',()=>{
 assert.equal(networkLinkLabel({link_speed_mbps:100,link_duplex:'full'}),'Link: 100 Mb/s · full duplex');
 assert.equal(networkLinkLabel({link_speed_mbps:1000}),'Link: 1 Gb/s');
 assert.equal(networkLinkLabel({link_rx_mbps:72.2,link_tx_mbps:65}),'Wi-Fi PHY: RX 72.2 Mb/s · TX 65 Mb/s');
 assert.equal(networkLinkLabel({link_speed_mbps:100},false),'Link speed unavailable');
 assert.equal(networkLinkLabel({link_speed_mbps:100},true,true),'Link: Not connected');
 assert.equal(networkLinkLabel({network_mbps:7}),'Link speed unavailable');
});
