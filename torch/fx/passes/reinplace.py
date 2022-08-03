import torch
from torch.fx import Node
from torch.fx._compatibility import compatibility
from torch._subclasses.fake_tensor import FakeTensorMode, FakeTensor
from torch.utils._pytree import tree_map
from torch.multiprocessing.reductions import StorageWeakRef

import _operator
from enum import Enum
import itertools
from typing import Set, Dict
from collections import defaultdict

__all__ = ['reinplace']

class _ViewType(Enum):
    NonView = 0
    SingleOutputView = 1
    MultiOutputView = 2

def _is_view_op(tgt):
    if tgt is not None and isinstance(tgt, torch._ops.OpOverload):
        schema = tgt._schema
        if len(schema.arguments) > 0:
            first_arg = schema.arguments[0]
            # check if op is a view
            return first_arg.alias_info is not None and not first_arg.alias_info.is_write

def _get_view_type(tgt) -> _ViewType:
    if tgt is not None and isinstance(tgt, torch._ops.OpOverload):
        schema = tgt._schema
        if len(schema.arguments) > 0:
            first_arg = schema.arguments[0]
            # check if op is a view
            if first_arg.alias_info is not None and not first_arg.alias_info.is_write:
                # check if op is a multi-output view
                if '*' in first_arg.alias_info.after_set:
                    return _ViewType.MultiOutputView
                else:
                    return _ViewType.SingleOutputView
    return _ViewType.NonView


# Stores a bunch of metadata related to functionalization each node.
# Relevant metadata:
# n.meta['fake_result']: FakeTensor (same type as the output of the node, but with FakeTenors instead of Tensors)
#   The fake tensor output from running the current node
# n.meta['view_of']: Node
#   If the current node n is a view of some base tensor, the 'view_of' field tells us which
#   view node was used to generate the current node (a view tensor).
#   This information actually makes `fake_result` redundant, but we can use `fake_result`
#   to sanity check that our aliasing information is correct.
@compatibility(is_backward_compatible=False)
class _FunctionalizationMetadataProp(torch.fx.Interpreter):

    def run_node(self, node: Node):
        self.node_counter += 1
        result = super().run_node(node)
        node.meta['fake_result'] = result
        node.meta['node_idx'] = self.node_counter

        # (1) Update metadata with the list of nodes that are used by this node
        # copy_() doesn't read from its first argument; it writes to it, overwriting previous data.
        # We don't want to treat it as "being used as an input".
        node_args = node.args
        if node.target is torch.ops.aten.copy_.default:
            node_args = node_args[1:]

        # (2) Update metadata to track aliasing information about view tensor nodes.
        if node.op == 'call_function':
            view_type = _get_view_type(node.target)
            if view_type == _ViewType.SingleOutputView:
                assert isinstance(node.args[0], Node)
                node.meta['view_of'] = node.args[0]
            elif view_type == _ViewType.MultiOutputView:
                self.multi_output_view_nodes[node] = node.args[0]

            # Check if we returned a multi-output view,
            # and we're now grabbing the individual views from the output.
            #
            # For multi-output views, we want to map each output view to the base,
            # but this mapping involves two separate nodes in FX IR.
            # e.g. "a, b = x_1.split(...)" becomes:
            #    %split_tensor : [#users=2] = call_function[target=torch.ops.aten.split.Tensor](args = (%x_1, 2), kwargs = {})
            #    %getitem : [#users=1] = call_function[target=operator.getitem](args = (%split_tensor, 0), kwargs = {})
            #    %getitem_1 : [#users=1] = call_function[target=operator.getitem](args = (%split_tensor, 1), kwargs = {})
            # And we'd like to set:
            #    getitem1.meta['view_of'] = x_1
            elif node.target is _operator.getitem:
                list_arg = node.args[0]
                maybe_base_of_view = self.multi_output_view_nodes.get(list_arg, None)
                if maybe_base_of_view is not None:
                    # Note: we could also track indexing info here for multi-output views.
                    # I don't think this metadata is strictly needed for de-functionalization.
                    assert isinstance(maybe_base_of_view, Node)
                    node.meta['view_of'] = maybe_base_of_view

        if 'view_of' in node.meta:
            # We're linking the current node with its first argument as views.
            # Assert here that this is actually the case, and their storages are the same.
            assert isinstance(node.meta['fake_result'], FakeTensor)
            assert isinstance(node.meta['view_of'].meta['fake_result'], FakeTensor)
            view_storage = StorageWeakRef(node.meta['fake_result'].storage())
            base_storage = StorageWeakRef(node.meta['view_of'].meta['fake_result'].storage())
            assert view_storage == base_storage
        return result



    def propagate(self, *args):
        self.multi_output_view_nodes = {}
        self.node_counter = -1
        with FakeTensorMode.push() as mode:
            fake_args = [mode.from_tensor(a) for a in args]
            return super().run(*fake_args)

def _schemas_match(functional_schema, inplace_schema):
    names_match = inplace_schema.name.endswith("_") and inplace_schema.name[:-1] == functional_schema.name
    arg_types_match = len(functional_schema.arguments) == len(inplace_schema.arguments) and all(
        a1.type == a2.type for a1, a2 in zip(functional_schema.arguments, inplace_schema.arguments))
    # for the inplace op, its first argument should be mutable
    assert inplace_schema.arguments[0].alias_info is not None and inplace_schema.arguments[0].alias_info.is_write
    # and its remaining arguments shouldn't be.
    assert all(a.alias_info is None for a in inplace_schema.arguments[1:])
    return names_match and arg_types_match

# TODO: this should be beefed up to be able to properly re-inplace with:
# - mutating ops (e.g. _fused_moving_avg_obs_fq_helper)
# - out= ops (e.g. angle -> angle.out)
# TODO: we should also figure this info out using torchgen.
def _maybe_get_inplace_op(op):
    # __module__ seems broken; it returns torch._ops.aten which doesn't exist
    if not isinstance(op, torch._ops.OpOverload):
        return None
    # Some view ops have inplace variants (as_strided_, etc),
    # but we do NOT want the reinplacing pass to directly add these into the program.
    # (they'll require extra special handling, aren't aren't really useful for perf anyway)
    if _is_view_op(op):
        return None
    op_namespace = op.__module__.split(".")[-1]
    op_base_name = op.overloadpacket.__name__
    maybe_namespace_module = getattr(torch.ops, op_namespace)
    maybe_inplace_op = None if maybe_namespace_module is None else getattr(maybe_namespace_module, f'{op_base_name}_', None)
    if maybe_inplace_op is None:
        return None

    inplace_overloads = [
        getattr(maybe_inplace_op, overload_name) for overload_name in maybe_inplace_op.overloads()
    ]
    inplace_overloads_with_matching_schemas = [
        f
        for f in inplace_overloads
        if _schemas_match(op._schema, f._schema)
    ]
    # This is for sanity: if foo() and foo_() are both operators,
    # we expect them to have compatible schemas.
    # (This is asserted by codegen for ATen, but might not be true
    # for other arbitrary operators).
    assert len(inplace_overloads_with_matching_schemas) == 1
    inplace_op = inplace_overloads_with_matching_schemas[0]
    return inplace_op

_VIEW_INVERSE_MAP = {
    torch.ops.aten.diagonal_scatter.default: torch.ops.aten.diagonal.default,
    torch.ops.aten.select_scatter.default: torch.ops.aten.select.int,
    torch.ops.aten.slice_scatter.default: torch.ops.aten.slice.Tensor,
    torch.ops.aten.as_strided_scatter.default: torch.ops.aten.as_strided.default,
}

# This function, given a set of set of (aliased) tensor nodes,
# Returns any nodes in the graph that *use* any of the aliases, that occur *after* op_index
# in the node ordering.
def _get_all_later_node_usages(tensor_aliases: Set[Node], op_index: int):
    def _add_if_tensor(x, set_):
        if isinstance(x, FakeTensor):
            set_.add(StorageWeakRef(x.storage()))

    nodes_used_after = set()
    for t in tensor_aliases:
        # get all nodes that use the current alias
        usage_nodes = t.users
        for n in usage_nodes:
            # We only care about usages after the current node
            if n.meta['node_idx'] <= op_index:
                continue
            # We also don't care about intermediate view ops.
            # They only matter if their output is then used elsewhere
            # (either in an out-of-place op, or as an output to the function).
            if n in tensor_aliases:
                if isinstance(n.target, torch._ops.OpOverload) or n.target == _operator.getitem:
                    continue
            nodes_used_after.add(n)
    return nodes_used_after

# Given an op that we're trying to re-inplace, "b = foo(a)",
# And given a {view}_scatter op that shows up later in the graph, "y = {view}_scatter(base, x, args...)"
# Then re-inplacing `foo()` would allow us to remove the `{view}_scatter` op entirely, IF:
# If there are any aliases in the alias_set(a) that satisfy:
# (1) The base of "alias", "alias_base", has the same size/stride/offset metadata as "base"
# (2) The output of running {view}(alias, args...) gives you the same size/stride/offset metadata
#     as "alias"
def _get_view_inverse_node_usages(later_node_usages: Set[Node], self_aliases: Set[Node]) -> Set[Node]:
    def matching_view_metadata(a, b):
        return a.size() == b.size() and \
            a.stride() == b.stride() and \
            a.storage_offset() == b.storage_offset()

    view_inverse_nodes = set()
    # Go through them in node order, so we can see chains of view_scatter ops.
    for n in sorted(later_node_usages, key=lambda x: x.meta['node_idx']):
        if n.target not in _VIEW_INVERSE_MAP:
            continue
        base = n.args[0]
        mutated_view = n.args[1]
        assert isinstance(base, Node)
        assert isinstance(base.meta['fake_result'], FakeTensor)
        assert isinstance(mutated_view, Node)
        assert isinstance(mutated_view.meta['fake_result'], FakeTensor)
        # Check that this view_inverse op actually corresponds to taking doing the inverse
        # of one of our existing self_alias nodes.
        original_view = _VIEW_INVERSE_MAP[n.target]
        for self_alias in self_aliases:
            # We're looking for some alias of the self arg, "alias",
            # that was created from some op `alias = foo(base, args...)`
            # such that the current _scatter op "inverts" that foo call.
            # We can check that by running the original op again, and checking that the strides match.
            if 'view_of' not in self_alias.meta:
                continue
            self_alias_base = self_alias.meta['view_of']
            try:
                # The we're trying to re-use the args from the view_scatter call inside of the corresponding
                # view op, which might throw. This just indicates that view_scatter op isn't a valid inverse
                # of the current alias we're looking at.
                view_replay_metadata = original_view(self_alias_base.meta['fake_result'], *n.args[2:], **n.kwargs)
                expected_metadata = self_alias.meta['fake_result']
                # If the alias and its base both have matching metadata, then this view_scatter op is valid to re-inplace.
                if matching_view_metadata(self_alias_base.meta['fake_result'], base.meta['fake_result']) and \
                        matching_view_metadata(view_replay_metadata, expected_metadata):
                    view_inverse_nodes.add(n)
            except Exception:
                continue

    return view_inverse_nodes


@compatibility(is_backward_compatible=True)
def reinplace(gm, *sample_args):
    """
    Given an fx.GraphModule, modifies it to perform "reinplacing",
    mutating the nodes of the graph.
    We look for out-of-place op call sites like `b = a.add(...)`,
    and convert them to be inplace (`b = a.add_(...)`),
    as long as the input to the current operator ("a") isn't re-used
    anywhere later in the graph.

    This pass currently expects to operate on a **functional, ATen** graph.
    This can be obtained by running `make_fx(functionalize(f))`.

    Sample inputs are needed to determine aliasing relationships of the inputs.
    In general, we can't reinplace node `b = a.add(...)` if "a" aliases any of the
    inputs to the program.

    Given a node "b = foo(a, ...)", the algorithm for re-inplacing is as follows:

    (1) Check if foo has a mutating variant. If not, move to the next node.

        Note that we ignore view ops (we don't bother to turn `as_strided()`
        into `as_strided_()`), as it complicates the algorithm and doesn't
        provide meaningful speedups.

        Currently, we also only check for an inplace op, `foo_`.
        Later, we should beef this up to check for out= or mutable ops.

    (2) Check if "a" is an alias of any of the program inputs.

        If it is, skip and move to the next node.
        Inplace'ing an op that would cause it to mutate a program is not sound,
        because that would be a side effect visible to the user.

        NOTE: there's a future optimization that we should make:
        if "a" is a (alias of a)  program input, but later in the program
        there is a node that looks like "a.copy_(...)",
        Then re-inplacing is ok to do - we are temporarily re-using a's buffer,
        which will later be overwritten by the copy_() call.

        This will be an important optimization to have for programs that mutate
        their inputs. It currently isn't implemented though.

    (3) Check that "a" and all of its outstanding aliases are not used anywhere
        later in the graph. If this is the case, then it's safe to re-inplace
        to "b = foo_(a)".

        There are a few caveats to this, explained in more detail below:
        (a) If "a" is used later as an argument to a view op, that is okay.
            It's only a problem if "a" (or that view) is later passed
            into a normal operator, or if it is returned as the program output.
        (b) If "a" is a repeat argument in `foo()`, then don't reinplace.
            Most ATen kernels don't make any guarantees that this is sound,
            e.g. if you do aten.mul_(a, a).
            So we'll just ban re-inplacing in this case.
            It's only a problem if "a" (or that view) is later passed
        (c) If "a" is used as an input into a view "inverse" / "scatter"
            operator, it is potentially fine to re-inplace
            (and remove that scatter operator from the graph).
            See below for a more detailed example.

        NOTE: there is an optimization in this step that is crucial
        to fully recovering performance from functionalization.

        Given this program:
        def f(x):
            a = torch.ops.aten.add(x, x)
            b = torch.ops.aten.diagonal(a)
            torch.ops.aten.fill_(b, 0)
            return d

        Functionalization will emit the following:
        def f(x):
            a = torch.ops.aten.add(x, x)
            b = torch.ops.aten.diagonal(a, 0, 1)
            b_updated = torch.ops.aten.fill(b, 0)
            a_updated = torch.ops.aten.diagonal_scatter(a, b_updated, 0, 1)
            return a_updated

        Ordinarily, we would not be able to reinplace the fill,
        because "b" aliases with "a" which is used by the diagonal_scatter call.

        "re-inplacing" is on the hook for figuring out that it is ok to
        completely, the expensive diagonal_scatter call, if we re-inplace the add().

        So, for every `alias in alias_set(a)`, instead of checking
        that "alias" is not used anywhere later in the graph,
        we check that
            EITHER:
          (a) alias is not used anywhere later in the graph
            OR:
          (b) alias is used exactly once later on in the graph,
              in the following op:

                out = foo_scatter(alias, x, args...)

              where the following must hold:
                (i) "foo_scatter" is the "inverse" operator for foo.
                    This only applies to "foo" ops that are view operators,
                    which view into a subset of the original tensor's memory.
                    In practice, there are ~4 operators where this applies:
                      diagonal -> diagonal_scatter
                      slice -> slice_scatter
                      select -> select_scatter
                      as_strided -> as_strided_scatter
                (ii) "args..." are the same between the foo() and foo_scatter() calls.

    (4) Finally, after converting "b = foo(a)" into "foo_(a)",
        we need to find all later nodes that use "b" as an argument
        and update them to take in "a" instead.

        Note that for the majority of inplace ops, this isn't actually necessary
        (because most inplace ops return "self" as their output).
        This isn't generally true for all mutable ops though, which is why
        we need to actually replace all of the arguments.

        We also need to update our metadata of Dict[StorageWeakRef, Set[Node]],
        That maps a given tensor storage to the set of all nodes that take in that storage
        as an input.
        Specifically, re-inplacing `b = foo(a)` causes "a" and "b"'s sets to get fused
        together.

    (5) Any "view_inverse/scatter" nodes that were identified as "it's ok to ignore them"
        during step (3) get manually deleted from the graph.
        Their outputs are no longer used, so technically standard DCE would be able
        to do this, but we can no longer run FX's DCE pass now that we have mutable
        ops in the graph.
    """
    _FunctionalizationMetadataProp(gm).propagate(*sample_args)

    # Useful debug printing
    # def _print(x):
    # if isinstance(x, FakeTensor):
    # print(f'fake_result: {StorageWeakRef(x.storage()).cdata}')

    # for n in gm.graph.nodes:
    # print(n.format_node())
    # if hasattr(n, 'meta'):
    # print(f'node_idx: {n.meta["node_idx"]}')
    # if 'fake_result' in n.meta:
    # tree_map(_print, n.meta['fake_result'])
    # if 'view_of' in n.meta:
    # print(f'view_of: {str(n.meta["view_of"])}')
    # print()

    # We need to know which nodes correspond to inputs (or their aliases)
    # so we know not to re-inplace them.
    # NOTE: later, we'll need to add an optimization for fully recovering performance
    # on programs that mutate inputs.
    input_storages = set(StorageWeakRef(node.meta['fake_result'].storage()) for node in gm.graph.nodes if node.op == 'placeholder')


    # We also need to know for a given node, what are all of its aliasing nodes.
    storage_to_nodes: Dict[StorageWeakRef, Set[Node]] = defaultdict(set)
    for n in gm.graph.nodes:
        if 'fake_result' in n.meta:
            # Tree-mapping because some ops can return lists of tensors.
            def _add_to_map(x):
                if isinstance(x, FakeTensor):
                    storage_to_nodes[StorageWeakRef(x.storage())].add(n)
            tree_map(_add_to_map, n.meta['fake_result'])

    # inplace-ify functional ops, subject to the constraints written below.
    all_later_view_inverse_node_usages = set()
    for idx, node in enumerate(gm.graph.nodes):
        if node.op == 'call_function':
            # Step 1: Check to see if this operator has an inplace variant.
            maybe_inplace_op = _maybe_get_inplace_op(node.target)
            if maybe_inplace_op is None:
                continue
            # This is a proxy check for ensuring that the first argument is "tensor-like"
            # (This should be the case for all ops with inplace variants in ATen,
            # although we technically don't have guarantees for custom ops).
            assert len(node.target._schema.arguments) > 0
            assert 'Tensor' in str(node.target._schema.arguments[0].type)

            # Step 2: ensure that the op we're trying to re-inplace isn't a program input.
            self_arg = node.args[0]
            self_arg_name = self_arg.name
            self_arg_storage = StorageWeakRef(self_arg.meta['fake_result'].storage())
            if self_arg_storage in input_storages:
                # TODO: later, add the optimization for handling `copy_()` calls in the graph.
                continue
            if len([x for x in node.args if x is self_arg]) > 1:
                # Step (3b) in the original description.
                # Calling stuff like aten.mul_(a, a) isn't guaranteed to be sound,
                # so we prevent re-inplacing in this case.
                continue

            self_arg_storage = StorageWeakRef(self_arg.meta['fake_result'].storage())
            curr_node_storage = StorageWeakRef(node.meta['fake_result'].storage())
            self_aliases = storage_to_nodes[self_arg_storage]

            # First, we find all later usages of any of the aliases of self_arg.
            later_node_usages = _get_all_later_node_usages(self_aliases, node.meta['node_idx'])
            # Then, we check if any of those later usages are actually view_scatter ops
            # that are safe to fully remove.
            later_view_inverse_node_usages = _get_view_inverse_node_usages(later_node_usages, self_aliases)

            # Step 3: Check to see if the input to the op is re-used later in the graph.
            # If not (same goes for its aliases), then this op is safe to re-in place.
            # This is a slightly roundabout way to check that there are no later usages of the current self argument.
            # (later_view_inverse_node_usages corresponds to "view_scatter" nodes that we are allowed to delete)
            can_reinplace = len(later_node_usages - later_view_inverse_node_usages) == 0
            if not can_reinplace:
                continue
            # Step 4: replace the current out-of-place op with its inplace variant.
            node.target = maybe_inplace_op
            # At this point, 'storage_to_nodes' will be stale.
            # Now that we're inplacing `b = foo(a)`, we need to effectively
            # union together the dict values for b and a's storage.
            # Hmm... morally I think we also want to keep the `fake_result` metadata
            # up to date here, but I'm not sure how easy it is to do.
            # Maybe it's fine to wait until the end of the pass to update it.
            storage_to_nodes[self_arg_storage].update(storage_to_nodes[curr_node_storage])
            storage_to_nodes[curr_node_storage].update(storage_to_nodes[self_arg_storage])

            # Need to remember the view_scatter view nodes we found so we can remove them alter.
            all_later_view_inverse_node_usages.update(later_view_inverse_node_usages)

            # Now that we've replaced b = a.foo() with a.foo_(),
            # We need to replace any later usages of "b" with "a"
            for old in itertools.chain([node], later_view_inverse_node_usages):
                new = old.args[0]
                nodes_to_update = [n for n in old.users if n.meta['node_idx'] > node.meta['node_idx']]
                for node_to_update in nodes_to_update:
                    new_args = []
                    for arg_idx, a in enumerate(node_to_update.args):
                        if a == old:
                            new_args.append(new)
                        else:
                            new_args.append(a)
                    new_kwargs = {}
                    for kwarg_idx, (k, v) in enumerate(node_to_update.kwargs.items()):
                        if isinstance(v, Node) and v.name == old.name:
                            new_kwargs[k] = new
                        else:
                            new_kwargs[k] = v
                    node_to_update.args = tuple(new_args)
                    node_to_update.kwargs = new_kwargs

                    old_ref = StorageWeakRef(old.meta['fake_result'].storage())
                    node_ref = StorageWeakRef(node_to_update.meta['fake_result'].storage())
                    if old_ref == node_ref:
                        # This will happen if we're updating a view op, e.g.
                        # e.g. replacing
                        #     x = view(old)
                        #     x = view(new)
                        # When that happens, we need to make sure to keep our
                        # storage mapping up to date.
                        new_ref = StorageWeakRef(new.meta['fake_result'].storage())
                        # Technically, "old_ref" and all its aliases will remain
                        # in our mapping.
                        # That should be fine though, since we deleted "old"
                        # from the graph at this point.
                        storage_to_nodes[node_ref].update(storage_to_nodes[new_ref])
                        storage_to_nodes[new_ref].update(storage_to_nodes[node_ref])

    # Step 5: delete any _scatter nodes that we de-functionalized
    # Need to take care not to delete any of these nodes until after *all* modifications
    # to the graph are finished.
    for to_delete in all_later_view_inverse_node_usages:
        gm.graph.erase_node(to_delete)


    gm.recompile()
    return gm
