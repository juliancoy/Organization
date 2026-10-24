"""Publish the tested repositories to GitHub and pin each run to pushed commits."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

REPOSITORIES=('.', 'OrgPortal', 'PIdP')


def github_url(value):
    match=re.fullmatch(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?',value)
    if not match:raise ValueError('Test sources require a GitHub origin without embedded credentials')
    return 'https://github.com/'+match[1]+'.git'


def source_digest(repositories):
    refs={name:{key:repo[key] for key in ('url','commit')} for name,repo in repositories.items()}
    return hashlib.sha256(json.dumps(refs,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def publish(root, ident, stopped, progress):
    records={}
    def git(path,*args):
        if stopped():raise InterruptedError('GitHub preparation stopped')
        result=subprocess.run(['git','-C',str(path),*args],capture_output=True,text=True,timeout=180,
                              env={**os.environ,'GIT_TERMINAL_PROMPT':'0'})
        if result.returncode:
            # Git errors can include credential helper output; retain a useful phase, never raw credentials.
            raise RuntimeError(path.name+': git '+args[0]+' failed; check repository access, conflicts and GitHub authentication')
        return result.stdout.rstrip('\n')
    # Validate every repository before making the first commit.
    prepared=[]
    ordered=[]
    def include(name):
        path=Path(root)/name
        entries=git(path,'ls-files','--stage','-z').split('\0')
        for entry in entries:
            if entry.startswith('160000 '):
                child=entry.split('\t',1)[1]
                nested=str(Path(name)/child)
                if (Path(root)/nested/'.git').exists():include(nested)
        ordered.append(name)
    for name in REPOSITORIES:include(name)
    for name in ordered:
        path=Path(root)/name
        branch=git(path,'symbolic-ref','--quiet','--short','HEAD')
        url=github_url(git(path,'remote','get-url','origin'))
        if github_url(git(path,'remote','get-url','--push','origin'))!=url:raise RuntimeError(name+': GitHub fetch and push origins differ')
        if git(path,'ls-files','-u'):raise RuntimeError(name+': unresolved Git conflicts')
        changes=git(path,'ls-files','--modified','--deleted','--others','--exclude-standard','-z').split('\0')
        staged=git(path,'diff','--cached','--name-only','-z').split('\0')
        files=sorted(set(changes+staged)-{''})
        for filename in files:
            parts=Path(filename).parts;leaf=Path(filename).name.lower()
            example=leaf.endswith(('.example','.sample','.template'))
            if (any(part in {'.runtime','.ssh','node_modules','.venv','__pycache__'} for part in parts)
                    or (leaf.startswith('.env') and not example)
                    or Path(filename).suffix.lower() in {'.pem','.key','.p12','.pfx','.sqlite','.sqlite3','.db','.har','.log','.keystore'}
                    or leaf in {'credentials.json','secrets.json','id_rsa','id_ed25519'}):
                raise RuntimeError(name+': sensitive or runtime file requires exclusion before publication: '+filename)
        prepared.append((name,path,branch,url,files))
    for name,path,branch,url,files in prepared:
        record={'url':url,'branch':branch,'dirty':False,'stage':'committing'}
        records[name]=record;progress(name,dict(record))
        if files or git(path,'status','--porcelain'):
            git(path,'add','-A','--','.')
            staged_now=set(git(path,'diff','--cached','--name-only','-z').split('\0'))-{''}
            if not staged_now.issubset(set(files)|{entry.split('\t',1)[1] for entry in git(path,'ls-files','--stage','-z').split('\0') if entry.startswith('160000 ')}):raise RuntimeError(name+': files changed while staging; inspect the index and start again')
            if git(path,'diff','--cached','--name-only'):
                git(path,'commit','-m','Synchronize distributed test run '+ident)
        record.update(commit=git(path,'rev-parse','HEAD'),stage='fetching')
        progress(name,dict(record))
        git(path,'fetch','--no-tags','origin','refs/heads/'+branch)
        remote=git(path,'rev-parse','FETCH_HEAD')
        # Reconcile only fast-forward history. Never reset, force-push, stash or invent a branch.
        relation=subprocess.run(['git','-C',str(path),'merge-base','--is-ancestor',remote,'HEAD'],capture_output=True)
        if relation.returncode:
            git(path,'merge','--ff-only',remote)
        if git(path,'status','--porcelain'):raise RuntimeError(name+': workspace changed during publication; start again after edits finish')
        record.update(commit=git(path,'rev-parse','HEAD'),stage='pushing')
        progress(name,dict(record))
        git(path,'push','origin','HEAD:refs/heads/'+branch)
        advertised=git(path,'ls-remote','origin','refs/heads/'+branch).split()
        if not advertised or advertised[0]!=record['commit']:raise RuntimeError(name+': GitHub branch changed during push verification')
        record.update(stage='pushed',commit_url=url.removesuffix('.git')+'/commit/'+record['commit'])
        progress(name,dict(record))
    return records
