import test from 'node:test';
import assert from 'node:assert/strict';
import {recordResourceHistory} from '../view/resource-history.js';
import {deviceLevels} from '../view/levels-model.js';
const now=1000000;
const rowsFor=(data,metrics,metric,time)=>deviceLevels(data,metrics,metric,time);
const host=(time,percent)=>({host:'worker.local',addresses:['10.42.0.2'],status:'live',sampled_at:time/1000,percent,cpus:4});

test('a new page restores background history with the original sample times',()=>{
 const histories={cpu:new Map()};
 const metrics={hosts:[host(now,70)],history:[{sampled_at:(now-240000)/1000,hosts:[host(now-240000,25)]},{sampled_at:(now-200000)/1000,hosts:[host(now-200000,40)]}]};
 recordResourceHistory(histories,{},metrics,now,rowsFor);
 assert.deepEqual(histories.cpu.get('worker.local'),[{time:now-240000,value:25},{time:now-200000,value:40},{time:now,value:70}]);
 recordResourceHistory(histories,{},metrics,now,rowsFor);
 assert.equal(histories.cpu.get('worker.local').length,3);
});
test('old frames expire and current gateway traffic is never backfilled into history',()=>{
 const histories={network:new Map()};
 const data={devices:[{ip:'10.42.0.3',mac:'aa'}],link_traffic:{updated:new Date(now).toISOString(),peers:{aa:{rx_mbps:3,tx_mbps:2}}}};
 const metrics={hosts:[],history:[{sampled_at:(now-400000)/1000,hosts:[]},{sampled_at:(now-20000)/1000,hosts:[]}]};
 recordResourceHistory(histories,data,metrics,now,rowsFor);
 assert.deepEqual(histories.network.get('aa'),[{time:now,value:5}]);
});


test('historical directional gateway samples form a continuous measured series',()=>{
 const histories={split:new Map()};
 const devices=[{ip:'10.42.0.3',mac:'aa'}];
 const network=time=>({devices,link_traffic:{updated:new Date(time).toISOString(),peers:{aa:{rx_mbps:3,tx_mbps:2}}}});
 const frames=[now-10000,now-5000].map(time=>({sampled_at:time/1000,hosts:[],network:network(time)}));
 const splitRows=(data,metrics,metric,time)=>['send','receive'].flatMap(direction=>deviceLevels(data,metrics,direction,time).map(row=>({...row,id:row.id+':'+direction})));
 recordResourceHistory(histories,network(now),{hosts:[],history:frames},now,splitRows);
 assert.deepEqual(histories.split.get('aa:receive').map(point=>point.value),[2,2,2]);
 assert.deepEqual(histories.split.get('aa:send').map(point=>point.value),[3,3,3]);
 // Genuine missing capture data remains a gap, rather than being interpolated.
 frames[1].network.link_traffic.capture_error=true;
 recordResourceHistory(histories,network(now),{hosts:[],history:frames},now,splitRows);
 assert.equal(histories.split.get('aa:receive')[1].value,null);
});
