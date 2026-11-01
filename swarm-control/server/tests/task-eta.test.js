import test from 'node:test';
import assert from 'node:assert/strict';
import {readEta} from '../view/task-display.js';
test('parallel ETA follows slowest unfinished input, ignoring completed work',()=>{
 const row={children:[{status:'completed'},{status:'running',read_eta_seconds:60},{status:'running',read_eta_seconds:180}]};
 assert.equal(readEta(row,1000).seconds,180);
 row.children.push({status:'queued'});
 assert.match(readEta(row,1000).text,/unavailable/);
});
test('stream estimates use current attempt elapsed time and bytes',()=>{
 const row={status:'running',attempt_started:100,completed_units:25,total_units:100};
 assert.equal(readEta(row,200).seconds,300);
 assert.match(readEta({...row,completed_units:0},200).text,/unavailable/);
 assert.match(readEta({...row,stale:true},200).text,/stale/);
 assert.match(readEta({...row,sampled_at:100},200).text,/stale/);
});
test('full input scan still awaits verification',()=>{
 assert.match(readEta({status:'running',completed_units:100,total_units:100,units_per_second:10},200).text,/awaiting verification/);
 assert.equal(readEta({status:'completed'},200).text,'Verified complete');
});
test('blocked workflows expose the cause and completed stages await no verification',()=>{
 assert.match(readEta({children:[{name:'Align chromosomes',status:'failed',detail:'memory'}]},100).text,/failed step: Align chromosomes/);
 assert.match(readEta({children:[{name:'Normalize',status:'not scheduled'}]},100).text,/unscheduled step: Normalize/);
 assert.match(readEta({status:'preparing'},100).text,/Preparing workers/);
});
test('execution estimates are labelled separately from read estimates',()=>{
 const stage={status:'running',execution_eta_seconds:120,eta_scope:'Mapping'};
 assert.match(readEta(stage,100).text,/Mapping ETA ~2 min/);
 assert.match(readEta({children:[stage,{name:'Report',status:'not scheduled'}]},100).text,/unscheduled step: Report/);
});
test('missing evidence and preparation are explicit rather than generic queue ETAs',()=>{
 assert.equal(readEta({status:'waiting_inputs'},100).text,'Waiting for required inputs');
 assert.equal(readEta({status:'partial',input_blockers:['confidence evidence']},100).text,'Partial results · waiting for required inputs');
 assert.match(readEta({status:'preparing',children:[{name:'old placeholder',status:'queued'}]},100).text,/Preparing workers/);
});
