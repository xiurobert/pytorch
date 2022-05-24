# Owner(s): ["oncall: quantization"]

import torch
from torch.testing._internal.common_utils import TestCase
from torch.ao.quantization.utils import get_fqn_to_example_inputs


class TestUtils(TestCase):
    def _test_get_fqn_to_example_inputs(self, M, example_inputs, expected_fqn_to_dim):
        m = M().eval()
        fqn_to_example_inputs = get_fqn_to_example_inputs(m, example_inputs)
        for fqn, expected_dims in expected_fqn_to_dim.items():
            assert fqn in expected_fqn_to_dim
            example_inputs = fqn_to_example_inputs[fqn]
            for example_input, expected_dim in zip(example_inputs, expected_dims):
                assert example_input.dim() == expected_dim

    def test_get_fqn_to_example_inputs_simple(self):
        class Sub(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear1 = torch.nn.Linear(5, 5)
                self.linear2 = torch.nn.Linear(5, 5)

            def forward(self, x):
                x = self.linear1(x)
                x = self.linear2(x)
                return x

        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear1 = torch.nn.Linear(5, 5)
                self.linear2 = torch.nn.Linear(5, 5)
                self.sub = Sub()

            def forward(self, x):
                x = self.linear1(x)
                x = self.linear2(x)
                x = self.sub(x)
                return x

        expected_fqn_to_dim = {
            "": (2,),
            "linear1": (2,),
            "linear2": (2,),
            "sub": (2,),
            "sub.linear1": (2,),
            "sub.linear2": (2,)
        }
        example_inputs = (torch.rand(1, 5),)
        self._test_get_fqn_to_example_inputs(M, example_inputs, expected_fqn_to_dim)

    def test_get_fqn_to_example_inputs_kwargs(self):
        class Sub(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear1 = torch.nn.Linear(5, 5)
                self.linear2 = torch.nn.Linear(5, 5)

            def forward(self, x, key1=torch.rand(1), key2=torch.rand(1)):
                x = self.linear1(x)
                x = self.linear2(x)
                return x

        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear1 = torch.nn.Linear(5, 5)
                self.linear2 = torch.nn.Linear(5, 5)
                self.sub = Sub()

            def forward(self, x):
                x = self.linear1(x)
                x = self.linear2(x)
                # only override `key2`, `key1` will use default
                x = self.sub(x, key2=torch.rand(1, 2))
                return x

        expected_fqn_to_dim = {
            "": (2,),
            "linear1": (2,),
            "linear2": (2,),
            # second arg is `key1`, which is using default argument
            # third arg is `key2`, override by callsite
            "sub": (2, 1, 2),
            "sub.linear1": (2,),
            "sub.linear2": (2,)
        }
        example_inputs = (torch.rand(1, 5),)
        self._test_get_fqn_to_example_inputs(M, example_inputs, expected_fqn_to_dim)
