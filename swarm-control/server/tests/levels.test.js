import test from 'node:test';
import assert from 'node:assert/strict';
import {deviceLevels} from '../view/levels-model.js';
const now=Date.now(),host={host:'laptop.local',observed_hostname:'laptop',addresses:['10.42.0.2'],status:'live',sampled_at:now/1000,network_mbps:3,ram_percent:54,disk_busy_percent:82};
const data={devices:[{ip:'10.42.0.2',mac:'aa'},{ip:'10.42.0.3',mac:'bb'}],link_traffic:{updated:new Date(now).toISOString(),peers:{aa:{rx_mbps:2,tx_mbps:2},bb:{rx_mbps:1,tx_mbps:2}}}};
test('SSH address identity deduplicates and passive peers cover other devices',()=>{const rows=deviceLevels(data,{hosts:[host]},'network',now);assert.equal(rows.length,2);assert.equal(rows[0].value,3);assert.equal(rows[1].value,3);});
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
