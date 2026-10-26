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
 assert.deepEqual(histories.network.get('aa'),[{time:now-20000,value:null},{time:now,value:5}]);
});
