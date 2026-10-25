import test from 'node:test';
import assert from 'node:assert/strict';
import {deviceLevels} from '../view/levels-model.js';
const now=Date.now(),host={host:'laptop.local',observed_hostname:'laptop',addresses:['10.42.0.2'],status:'live',sampled_at:now/1000,network_mbps:3,ram_percent:54,disk_busy_percent:82};
const data={devices:[{ip:'10.42.0.2',mac:'aa'},{ip:'10.42.0.3',mac:'bb'}],link_traffic:{updated:new Date(now).toISOString(),peers:{aa:{rx_mbps:2,tx_mbps:2},bb:{rx_mbps:1,tx_mbps:2}}}};
test('SSH address identity deduplicates and passive peers cover other devices',()=>{const rows=deviceLevels(data,{hosts:[host]},'network',now);assert.equal(rows.length,2);assert.equal(rows[0].value,3);assert.equal(rows[1].value,3);});
test('unmeasured RAM is unknown and stale hosts lose measurements',()=>{assert.deepEqual(deviceLevels(data,{hosts:[host]},'ram',now).map(r=>r.value),[54,null]);assert.equal(deviceLevels(data,{hosts:[host]},'disk',now+16000)[0].value,null);});
test('unnamed device cannot match unavailable host with no observed hostname',()=>{const rows=deviceLevels(data,{hosts:[{host:'other.local',status:'unavailable'}]},'ram',now);assert.equal(rows.length,3);});
