"""Small typed rewrite space, inspired by ROVER; not an e-graph implementation.

Rules propose alternatives, never certify four-state equivalence. The existing
whole-design evaluator remains authoritative. No generated declarations/state.
"""
from copy import deepcopy


def leaves(tree):
    if type(tree) is int:
        return [tree]
    if not isinstance(tree, list) or len(tree) != 2:
        raise ValueError('sum tree nodes must be two-element lists or integer leaves')
    return leaves(tree[0]) + leaves(tree[1])


def tree_for(order, shape):
    if len(order) == 1:
        return order[0]
    k = {'balanced': len(order)//2, 'left': len(order)-1, 'right': 1}[shape]
    return [tree_for(order[:k], shape), tree_for(order[k:], shape)]


def initial(design):
    return ({'tree': tree_for(list(range(8)), 'left'), 'width': 16}
            if design == 'mul8' else {'groups': [1, 2, 3, 4], 'width': 1})


def catalog(design):
    if design == 'mul8':
        return [
            {'op': 'sum_tree', 'parameters': {'shape': ['balanced', 'left', 'right'],
             'order': 'permutation of integers 0..7'},
             'preconditions': 'eight conditional 16-bit partial products; preserve B === 1 gating'},
            {'op': 'rotate', 'parameters': {'path': 'list of 0/1 edges, at most 6',
             'direction': ['left', 'right']}, 'preconditions': 'rotation pivot is an addition node'}]
    return [{'op': 'carry_factor', 'parameters': {'groups': 'four integers; element i in 1..i+1'},
             'preconditions': 'one-bit XOR/AND carry equations; preserve original XOR semantics'}]


def apply(design, parent, action):
    if not isinstance(action, dict) or set(action) != {'op', 'params'}:
        raise ValueError('expected exactly op and params')
    op, p = action['op'], action['params']
    if not isinstance(p, dict):
        raise ValueError('params must be an object')
    if design == 'mul8':
        if op == 'sum_tree' and set(p) == {'shape', 'order'}:
            order = p['order']
            if (p['shape'] not in ('balanced', 'left', 'right') or not isinstance(order, list)
                    or any(type(x) is not int for x in order) or sorted(order) != list(range(8))):
                raise ValueError('invalid shape or partial-product permutation')
            tree = tree_for(order, p['shape'])
        elif op == 'rotate' and set(p) == {'path', 'direction'}:
            path = p['path']
            if (not isinstance(path, list) or len(path) > 6
                    or any(type(x) is not int or x not in (0, 1) for x in path)
                    or p['direction'] not in ('left', 'right')):
                raise ValueError('invalid rotation')
            tree = deepcopy(parent['tree'])
            node = tree
            for edge in path:
                if not isinstance(node, list):
                    raise ValueError('rotation path leaves tree')
                node = node[edge]
            if not isinstance(node, list):
                raise ValueError('rotation target is a leaf')
            a, b = node
            if p['direction'] == 'left' and isinstance(b, list):
                node[:] = [[a, b[0]], b[1]]
            elif p['direction'] == 'right' and isinstance(a, list):
                node[:] = [a[0], [a[1], b]]
            else:
                raise ValueError('rotation pivot is a leaf')
        else:
            raise ValueError('unsupported sum-tree action')
        if sorted(leaves(tree)) != list(range(8)):
            raise ValueError('partial products must occur exactly once')
        state = {'tree': tree, 'width': 16}
        def emit(t):
            if type(t) is int:
                return f"((B[{t}] === 1'b1) ? ({{8'b0,multiplicand}} << (shift_count + 4'd{t})) : 16'b0)"
            return f'({emit(t[0])} + {emit(t[1])})'
        block = ('always @* begin multiplicand=A; shift_count=0; product='
                 + emit(tree) + '; shift_count=8; end')
    elif design == 'cla32' and op == 'carry_factor' and set(p) == {'groups'}:
        groups = p['groups']
        if (not isinstance(groups, list) or len(groups) != 4
                or any(type(g) is not int or not 1 <= g <= i for i, g in enumerate(groups, 1))):
            raise ValueError('invalid carry grouping')
        def carry(n, group):
            if n == 0:
                return 'c0'
            k = min(n, group)
            terms = []
            for j in range(k):
                terms.append('(' + ' & '.join([f'p{i}' for i in range(n, n-j, -1)] + [f'g{n-j}']) + ')')
            terms.append('(' + ' & '.join([f'p{i}' for i in range(n, n-k, -1)] + [carry(n-k, group)]) + ')')
            return '(' + ' ^ '.join(terms) + ')'
        block = '\n'.join(f'assign c{i} = {carry(i, g)};' for i, g in enumerate(groups, 1))
        state = {'groups': groups, 'width': 1}
    else:
        raise ValueError('unsupported action/design')
    return state, block


def demo_actions(design):
    if design == 'mul8':
        balanced = {'op': 'sum_tree', 'params': {'shape': 'balanced', 'order': list(range(8))}}
        return [balanced, balanced,
                {'op': 'rotate', 'params': {'path': [], 'direction': 'right'}},
                {'op': 'sum_tree', 'params': {'shape': 'balanced', 'order': [0,2,4,6,1,3,5,7]}},
                {'op': 'sum_tree', 'params': {'shape': 'right', 'order': list(range(8))}},
                {'op': 'sum_tree', 'params': {'shape': 'balanced', 'order': [0]*8}}]
    return [{'op': 'carry_factor', 'params': {'groups': g}} for g in
            ([1,1,1,1], [1,1,1,1], [1,2,2,2], [1,2,3,4], [1,1,2,3], [1,2,3,5])]
