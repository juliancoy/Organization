import test from 'node:test';
import assert from 'node:assert/strict';
import {currentPerformance,memorySeries,sortedRows} from '../view/monitor-model.js';
test('AMD cache events never become DRAM bandwidth and stale samples clear',()=>{
 const host={performance:{schema_version:1,sampled_at:100,memory:{cache:{'cache references':1000,'cache misses':100},dram_read_bytes_per_second:null}}};
 const rows=memorySeries(host,105);
 assert.equal(rows.find(r=>r.key==='cache misses').value,100);
 assert.equal(rows.find(r=>r.key==='dram_read_bytes_per_second').value,null);
 assert.equal(currentPerformance(host,116),null);
});
test('numeric values sort numerically and missing rows remain last in both directions',()=>{
 assert.deepEqual(sortedRows([[10],[2],[null]],0,false),[[2],[10],[null]]);
 assert.deepEqual(sortedRows([[10],[2],[null]],0,true),[[10],[2],[null]]);
});
test('future and explicit stale telemetry cannot look current',()=>{
 assert.equal(currentPerformance({performance:{schema_version:1,sampled_at:110}},100),null);
 assert.equal(currentPerformance({performance:{schema_version:1,sampled_at:100,stale:true}},105),null);
});
