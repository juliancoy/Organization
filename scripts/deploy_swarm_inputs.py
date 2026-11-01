"""Roll input runtimes and launcher updates through the standard machine deployer.

--watch retries offline/busy machines; active work is never interrupted.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'swarm-control/server'))
from agent_client import AgentClient,CONFIG
from agent_updates import LauncherUpdates
from input_runtime_release import InputRuntimeRelease


def sweep():
    config=json.loads(CONFIG.read_text());updater=LauncherUpdates(ROOT,lambda:False,lambda *args:None)
    runtime=InputRuntimeRelease(ROOT);rows=[]
    for name,address in config['peers'].items():
        host={'host':name,'local':name==config['name'],'lan_address':address,'agent_endpoint':'http://'+address+':8790'}
        client=AgentClient(host);row={'node':name}
        try:
            status=client.call('status',timeout=4)
            if status['node']!=name:raise ValueError('Agent identity mismatch')
            row['inputs']=runtime.stage(updater,host,status)
            status=updater.ensure(host,client,status)
            row.update(status='current',launcher_revision=status['source_revision'],ready=status['ready'])
        except Exception as error:
            row.update(status='pending',reason=str(error)[:180])
        finally:client.close()
        rows.append(row);print(json.dumps(row),flush=True)
    report={'updated':time.time(),'launcher_revision':updater.revision,'input_revision':runtime.revision,'nodes':rows}
    folder=Path.home()/'.local/state/swarm-control';folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    temp=folder/'input-deployment.tmp';temp.write_text(json.dumps(report,indent=2));temp.chmod(0o600);temp.replace(folder/'input-deployment.json')
    return all(row['status']=='current' for row in rows)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--watch',action='store_true');args=parser.parse_args()
    os.umask(0o077)
    while True:
        done=sweep()
        if done:return
        if not args.watch:raise SystemExit(1)
        time.sleep(30)

if __name__=='__main__':main()
