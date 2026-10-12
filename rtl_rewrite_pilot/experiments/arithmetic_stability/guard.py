"""Restrict changes to frozen arithmetic region and existing signals; tools prove semantics."""
import re
SPEC=None
TOKEN=re.compile(r"\s*(\d+'[bBdDhHoO][0-9a-fA-F_]+|'[01]|[A-Za-z_][A-Za-z0-9_]*|\d+|===|!==|==|!=|<=|>=|<<|>>|&&|\|\||[()\[\]{}?:;,@*~!&|^=<>+\-/])")
def configure(d):
    global SPEC
    SPEC=d

def check(text):
    try:
        if len(text.encode())>6000 or any(s in text for s in ('`','//','/*','$')):raise ValueError('forbidden text/oversize')
        if re.search(r'\(\*\s*[A-Za-z_]',text):raise ValueError('attributes forbidden')
        ts=[];i=0;text=text.strip()
        while i<len(text):
            m=TOKEN.match(text,i)
            if not m:raise ValueError('unsupported token')
            ts.append(m[1]);i=m.end()
        clean=' '.join(ts)
        if SPEC['stem']=='cla32':
            names={'assign','c0','c1','c2','c3','c4',*[f'{x}{n}' for x in ('p','g') for n in range(1,5)]}
            if any(t[0].isalpha() and t not in names for t in ts):raise ValueError('undeclared or forbidden identifier')
            # Every assignment is continuous, only c1..c4; no declarations or module text.
            chunks=clean.split(';');targets=[]
            for chunk in chunks:
                if not chunk.strip():continue
                if not chunk.strip().startswith('assign '):raise ValueError('continuous assignments only')
                for part in chunk.strip()[7:].split(','):
                    m=re.fullmatch(r'(c[1-4])\s*=\s*(.+)',part.strip())
                    if not m or '=' in m[2]:raise ValueError('carry target/expression')
                    targets.append(m[1])
            if sorted(targets)!=['c1','c2','c3','c4']:raise ValueError('assign each carry exactly once')
        else:
            if not re.match(r'always\s+@\s*(\*|\(\s*\*\s*\))',clean):raise ValueError('one always @* block')
            names={'always','begin','end','if','else','for','int','i','A','B','product','multiplicand','shift_count'}
            if any(t[0].isalpha() and t not in names for t in ts):raise ValueError('forbidden identifier or statement')
            if ts.count('always')!=1 or ts.count('for')>1:raise ValueError('one block; at most one fixed loop')
            loop=r'for \( int i = 0 ; i < 8 ; i = i \+ 1 \)'
            residual,n=re.subn(loop,'',clean)
            if ts.count('for')!=n or ('int' in residual.split()):raise ValueError('only original fixed eight-iteration loop')
            for j,t in enumerate(residual.split()):
                if t=='=' and residual.split()[j-1] not in ('product','multiplicand','shift_count'):raise ValueError('write outside original targets')
            if '<=' in ts:raise ValueError('nonblocking assignment not allowed')
        return True,clean
    except (ValueError,IndexError) as ex:return False,str(ex)
