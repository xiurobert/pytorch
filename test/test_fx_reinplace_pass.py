# Owner(s): ["module: functionalization"]
import torch
from torch.testing._internal.common_utils import TestCase, run_tests
from torch.fx.passes.reinplace import reinplace
from torch.fx.experimental.proxy_tensor import make_fx

try:
    from functorch.experimental import functionalize
    HAS_FUNCTIONALIZATION = True
except Exception as e:
    HAS_FUNCTIONALIZATION = False

class TestReinplacePass(TestCase):

    def test_reinplace_basic(self):
        # Basic test: the out-of-place add() call should be converted
        # into add_()
        def f(x):
            a = x.clone()
            b = a.add(1)
            return b

        inpt = torch.ones(2)
        f2 = reinplace(make_fx(f)(inpt), inpt)
        expected_out = f(inpt)
        actual_out = f2(inpt)
        self.assertEqual(actual_out, expected_out)
        self.assertExpectedInline(f2.code, """\



def forward(self, x_1):
    clone_default = torch.ops.aten.clone.default(x_1);  x_1 = None
    add_tensor = torch.ops.aten.add_.Tensor(clone_default, 1)
    return clone_default
    """)


    def test_reinplace_with_view(self):
        def f(x):
            a = x.clone()
            a_view = a.view(-1)
            # We shouldn't re-inplace the first add(), because an alias of a is re-used later in the program
            b = a.add(1)
            # Second add() is fine to re-inplace
            c = a_view.add(1)
            return c

        inpt = torch.ones(2)
        f2 = reinplace(make_fx(f)(inpt), inpt)
        expected_out = f(inpt)
        actual_out = f2(inpt)
        self.assertEqual(actual_out, expected_out)
        self.assertExpectedInline(f2.code, """\



def forward(self, x_1):
    clone_default = torch.ops.aten.clone.default(x_1);  x_1 = None
    view_default = torch.ops.aten.view.default(clone_default, [-1])
    add_tensor = torch.ops.aten.add.Tensor(clone_default, 1);  clone_default = None
    add_tensor_1 = torch.ops.aten.add_.Tensor(view_default, 1)
    return view_default
    """)

    # This test won't actually run in CI, because it requires functionalize() from functorch.
    # I'm planning on testing more comprehensively with torchbench models,
    # but we can make this testing better once functorch moves into pytorch/pytorch.
    def test_reinplace_scatter_op(self):
        def f(a_):
            # for now, don't test mutations to inputs
            a = a_.clone()
            e = a.view(-1)
            b = a.view(-1)
            c = b[0]
            d = c.view(-1)
            d.add_(1)
            return a + e

        if not HAS_FUNCTIONALIZATION:
            return
        inpt = torch.ones(4)
        f2 = reinplace(make_fx(functionalize(f))(inpt), inpt)
        expected_out = f(inpt)
        actual_out = f2(inpt)
        self.assertEqual(actual_out, expected_out)
        # NOTE: one slight pessimization here is the fact that
        # there are a bunch of redundant views in the graph.
        # Technically, half of these views are duplicates that we could de-dup.
        # This shouldn't really hurt performance though, since creating an extra view
        # is effectively just moving some metadata around (and allocating a new TensorImpl).
        # We can/should update the pass in the future to clean this up.
        self.assertExpectedInline(f2.code, """\



def forward(self, a__1):
    clone_default = torch.ops.aten.clone.default(a__1);  a__1 = None
    view_default = torch.ops.aten.view.default(clone_default, [-1])
    view_default_1 = torch.ops.aten.view.default(clone_default, [-1])
    select_int = torch.ops.aten.select.int(view_default_1, 0, 0);  view_default_1 = None
    view_default_2 = torch.ops.aten.view.default(select_int, [-1]);  select_int = None
    add_tensor = torch.ops.aten.add_.Tensor(view_default_2, 1)
    view_default_3 = torch.ops.aten.view.default(clone_default, [-1]);  clone_default = None
    select_int_1 = torch.ops.aten.select.int(view_default_3, 0, 0)
    view_default_4 = torch.ops.aten.view.default(view_default_2, []);  view_default_2 = None
    view_default_5 = torch.ops.aten.view.default(view_default_3, [4]);  view_default_3 = None
    view_default_6 = torch.ops.aten.view.default(view_default_5, [-1])
    add_tensor_1 = torch.ops.aten.add_.Tensor(view_default_5, view_default_6);  view_default_6 = None
    return view_default_5
    """)

    def test_reinplace_scatter_twice(self):
        def f(a_):
            # for now, don't test mutations to inputs
            a = a_.clone()
            b = a[:, 1]
            c = b[1]
            c.add_(1)
            return a

        if not HAS_FUNCTIONALIZATION:
            return

        inpt = torch.ones(4, 4)
        f2 = reinplace(make_fx(functionalize(f))(inpt), inpt)
        expected_out = f(inpt)
        actual_out = f2(inpt)
        self.assertEqual(actual_out, expected_out)
        self.assertExpectedInline(f2.code, """\



def forward(self, a__1):
    clone_default = torch.ops.aten.clone.default(a__1);  a__1 = None
    slice_tensor = torch.ops.aten.slice.Tensor(clone_default, 0, 0, 9223372036854775807)
    select_int = torch.ops.aten.select.int(slice_tensor, 1, 1);  slice_tensor = None
    select_int_1 = torch.ops.aten.select.int(select_int, 0, 1);  select_int = None
    add_tensor = torch.ops.aten.add_.Tensor(select_int_1, 1);  select_int_1 = None
    slice_tensor_1 = torch.ops.aten.slice.Tensor(clone_default, 0, 0, 9223372036854775807)
    select_int_2 = torch.ops.aten.select.int(slice_tensor_1, 1, 1);  slice_tensor_1 = None
    return clone_default
    """)

    def test_reinplace_scatter_twice_with_different_view_op_valid(self):
        def f(a_):
            a = a_.clone()
            b = a[:, 1]
            c = b[1]
            c_updated = c.add(1)
            good_mirror_of_b = a.as_strided((4,), (4,), 1)
            # good_mirror_of_b points to the same region of memory as b.
            # and this scatter op below tries to scatter c_updated into the same region
            # that c currently takes up.
            # reinplacing logic checks this by confirming that:
            #   c_updated
            #   good_mirror_of_b.select(0, 1)
            # have the same size/stride/storage_offset.
            b_updated = torch.select_scatter(good_mirror_of_b, c_updated, 0, 1)
            return b_updated

        inpt = torch.ones(4, 4)
        f2 = reinplace(make_fx(f)(inpt), inpt)
        expected_out = f(inpt)
        actual_out = f2(inpt)
        self.assertEqual(actual_out, expected_out)
        self.assertExpectedInline(f2.code, """\



def forward(self, a__1):
    clone_default = torch.ops.aten.clone.default(a__1);  a__1 = None
    slice_tensor = torch.ops.aten.slice.Tensor(clone_default, 0, 0, 9223372036854775807)
    select_int = torch.ops.aten.select.int(slice_tensor, 1, 1);  slice_tensor = None
    select_int_1 = torch.ops.aten.select.int(select_int, 0, 1);  select_int = None
    add_tensor = torch.ops.aten.add_.Tensor(select_int_1, 1);  select_int_1 = None
    as_strided_default = torch.ops.aten.as_strided.default(clone_default, [4], [4], 1);  clone_default = None
    return as_strided_default
    """)

    # Test example where we have a scatter op, where the base tensor
    # has the same size/stride/storage offset (even though it is a different view),
    # making it valid to re-inplace
    def test_reinplace_scatter_twice_with_different_view_op_invalid(self):
        def f(a_):
            a = a_.clone()
            b = a[:, 1]
            c = b[1]
            c_updated = c.add(1)
            good_mirror_of_b = a.as_strided((4,), (4,), 1)
            # The first arg to select_scatter is an equivalent view to b.
            # However, the select_scatter call below tries to put c_updated
            # into a different slice of "b" than what "c" currently occupies.
            #
            b_updated = torch.select_scatter(good_mirror_of_b, c_updated, 0, 0)
            return b_updated

        inpt = torch.ones(4, 4)
        f2 = reinplace(make_fx(f)(inpt), inpt)
        expected_out = f(inpt)
        actual_out = f2(inpt)
        self.assertEqual(actual_out, expected_out)
        self.assertExpectedInline(f2.code, """\



def forward(self, a__1):
    clone_default = torch.ops.aten.clone.default(a__1);  a__1 = None
    slice_tensor = torch.ops.aten.slice.Tensor(clone_default, 0, 0, 9223372036854775807)
    select_int = torch.ops.aten.select.int(slice_tensor, 1, 1);  slice_tensor = None
    select_int_1 = torch.ops.aten.select.int(select_int, 0, 1);  select_int = None
    add_tensor = torch.ops.aten.add.Tensor(select_int_1, 1);  select_int_1 = None
    as_strided_default = torch.ops.aten.as_strided.default(clone_default, [4], [4], 1);  clone_default = None
    select_scatter_default = torch.ops.aten.select_scatter.default(as_strided_default, add_tensor, 0, 0);  as_strided_default = add_tensor = None
    return select_scatter_default
    """)  # noqa: B950

    def test_reinplace_scatter_twice_with_different_view_op_invalid2(self):
        def f(a_):
            a = a_.clone()
            b = a[:, 1]
            c = b[1]
            c_updated = c.add(1)
            bad_mirror_of_b = a.as_strided((4,), (4,), 0)
            # The first arg to select_scatter points to a different than c's base.
            # This makes it invalid to re-inplace.
            b_updated = torch.select_scatter(bad_mirror_of_b, c_updated, 0, 1)
            return b_updated

        inpt = torch.ones(4, 4)
        f2 = reinplace(make_fx(f)(inpt), inpt)
        expected_out = f(inpt)
        actual_out = f2(inpt)
        # self.assertEqual(actual_out, expected_out)
        self.assertExpectedInline(f2.code, """\



def forward(self, a__1):
    clone_default = torch.ops.aten.clone.default(a__1);  a__1 = None
    slice_tensor = torch.ops.aten.slice.Tensor(clone_default, 0, 0, 9223372036854775807)
    select_int = torch.ops.aten.select.int(slice_tensor, 1, 1);  slice_tensor = None
    select_int_1 = torch.ops.aten.select.int(select_int, 0, 1);  select_int = None
    add_tensor = torch.ops.aten.add.Tensor(select_int_1, 1);  select_int_1 = None
    as_strided_default = torch.ops.aten.as_strided.default(clone_default, [4], [4], 0);  clone_default = None
    select_scatter_default = torch.ops.aten.select_scatter.default(as_strided_default, add_tensor, 0, 1);  as_strided_default = add_tensor = None
    return select_scatter_default
    """)  # noqa: B950

if __name__ == '__main__':
    run_tests()
