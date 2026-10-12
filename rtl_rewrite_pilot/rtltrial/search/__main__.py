"""python3 -m rtltrial.search --help"""
import argparse
import fcntl
import json
import sys
from pathlib import Path
from . import engine, rules


def main():
    p = argparse.ArgumentParser(description='Structural RTL candidate pools; local tools only, no provider calls.')
    sub = p.add_subparsers(dest='command', required=True)
    for command in ('prepare','next','submit','abort','summarize','finalize','demo'):
        q = sub.add_parser(command)
        q.add_argument('--root', type=Path, required=True)
        if command in ('prepare','demo'):
            q.add_argument('--design', choices=['mul8','cla32'], required=True)
            q.add_argument('--pool-size', type=int, default=4)
        if command == 'prepare':
            q.add_argument('--generation', choices=['structured','freeform'], default='structured')
            q.add_argument('--attempts', type=int, default=12)
        if command in ('next','submit','abort'):
            q.add_argument('--arm', choices=['B1','B2'], required=True)
        if command == 'submit':
            group=q.add_mutually_exclusive_group(required=True)
            group.add_argument('--response',type=Path)
            group.add_argument('--error')
    a = p.parse_args()
    root = a.root.resolve()
    root.parent.mkdir(parents=True,exist_ok=True)
    # Lock beside root so prepare still requires a genuinely fresh directory.
    with (root.parent / ('.'+root.name+'.lock')).open('w') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            p.error('another process owns this run')
        if a.command == 'prepare':
            result=engine.prepare(root,a.design,a.generation,a.attempts,a.pool_size)
        elif a.command == 'next':
            result=engine.reserve(root,a.arm)
            result={'id':result['id'],'parent':result['parent'], 'request':str(root/'attempts'/result['id']/'request.json')}
        elif a.command == 'submit':
            result=engine.submit(root,a.arm,a.response.read_text() if a.response else None,a.error)
            result={k:result.get(k) for k in ('id','decision','reason')}
        elif a.command == 'abort':
            result=engine.abort(root,a.arm)
        elif a.command == 'summarize':
            result=engine.summarize(root)
        elif a.command == 'finalize':
            result=engine.finalize(root)
        else:
            actions=rules.demo_actions(a.design)
            engine.prepare(root,a.design,'structured',len(actions),a.pool_size)
            for action in actions:
                for arm in ('B1','B2'):
                    r=engine.reserve(root,arm)
                    r=engine.submit(root,arm,json.dumps(action))
                    print(json.dumps({k:r.get(k) for k in ('id','parent','decision','reason')}),flush=True)
            result=engine.finalize(root)
        print(json.dumps(result,indent=2))
        if result.get('status') == 'FAIL':
            return 1
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError,FileNotFoundError) as ex:
        sys.exit(str(ex))
