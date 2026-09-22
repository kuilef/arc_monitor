"""Prepare an initial Git commit; add --push to publish via your own local Git authorization.

No tokens, force push, global config edits, API calls or remote deletion.
Only known project paths are added. Existing unrelated repositories/remotes are rejected.
"""
from pathlib import Path
import argparse
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
REMOTE='https://github.com/kuilef/arc_monitor.git'
PATHS=['README.md','pyproject.toml','requirements-tested.txt','.gitignore','.dockerignore',
       '.env.example','Dockerfile','compose.yaml','.github','src','tests','docs','examples','scripts']


def git(*args,check=True):
    return subprocess.run(['git',*args],cwd=ROOT,text=True,capture_output=True,check=check)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--push',action='store_true',help='Publish HEAD to main; ordinary non-force push only')
    args=parser.parse_args()
    try:
        existing=git('rev-parse','--show-toplevel',check=False)
        if existing.returncode==0 and Path(existing.stdout.strip()).resolve()!=ROOT:
            raise ValueError('Project is inside another Git repository. Move the project to a separate folder.')
        if existing.returncode!=0:
            git('init','-b','main')
        remote=git('remote','get-url','origin',check=False)
        if remote.returncode==0:
            accepted=[REMOTE,REMOTE.removesuffix('.git'),'git@github.com:kuilef/arc_monitor.git']
            if remote.stdout.strip() not in accepted:
                raise ValueError('Existing origin differs from the requested repository; refusing to change it.')
        else:
            git('remote','add','origin',REMOTE)
        git('add','--',*[p for p in PATHS if (ROOT/p).exists()])
        changed=git('diff','--cached','--quiet',check=False)
        if changed.returncode==1:
            git('commit','-m','feat: Arc Monitor research-first MVP with tests and local dashboard')
        elif changed.returncode!=0:
            raise ValueError('Cannot inspect staged changes')
        print(git('log','-1','--format=%h %s').stdout.strip())
        if args.push:
            # Existing remote history is protected by Git's default fast-forward requirement.
            result=git('push','-u','origin','HEAD:refs/heads/main')
            print(result.stdout,result.stderr)
        else:
            print('Local commit prepared. Run: python scripts/publish.py --push')
        return 0
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or str(exc),file=sys.stderr)
        print('Check local Git name/email and GitHub sign-in. No force push was attempted.',file=sys.stderr)
        return 2
    except (OSError,ValueError) as exc:
        print(str(exc),file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
