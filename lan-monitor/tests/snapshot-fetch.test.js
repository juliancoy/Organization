import test from 'node:test';
import assert from 'node:assert/strict';
import {createSnapshotFetch} from '../view/snapshot-fetch.js';

test('panels share in-flight and recent snapshots with independent bodies', async () => {
 let calls=0, clock=0;
 const read=createSnapshotFetch(new Map(),async()=>{calls++;return new Response('{"devices":[]}');},()=>clock);
 const responses=await Promise.all([read('/api/devices?resolve=0'),read('/api/devices?resolve=0')]);
 assert.deepEqual(await Promise.all(responses.map(r=>r.json())),[{devices:[]},{devices:[]}]);
 assert.equal(calls,1);
 clock=2999;await read('/api/devices?resolve=0');assert.equal(calls,1);
 clock=3000;await read('/api/devices?resolve=0');assert.equal(calls,2);
});
test('failed reads retry and mutations never use cached responses',async()=>{
 let calls=0;
 const read=createSnapshotFetch(new Map(),async()=>new Response('body',{status:++calls===1?503:200}));
 assert.equal((await read('/api/cpu')).status,503);
 assert.equal((await read('/api/cpu')).status,200);
 await read('/api/cpu',{method:'POST'});await read('/api/cpu',{method:'POST'});
 assert.equal(calls,4);
});
test('selected historical runs and name-resolution preferences remain distinct',async()=>{
 let calls=0;const read=createSnapshotFetch(new Map(),async()=>{calls++;return new Response('{}');});
 await read('/api/distributed-tests?id=a');await read('/api/distributed-tests?id=b');
 await read('/api/devices?resolve=0');await read('/api/devices?resolve=1');assert.equal(calls,4);
});
