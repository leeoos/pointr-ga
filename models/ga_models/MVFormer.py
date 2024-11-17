import torch
import torch.nn as nn
import torch.nn.functional as F

from utils.ga_utils import fast_einsum, unsqueeze_like

# Clifford algebra and modules 
from clifford_lib.algebra.cliffordalgebra import CliffordAlgebra
from clifford_modules.MVLinear import MVLinear


class NormalizationLayer(nn.Module):
    """
    Normalization layer to scale down the elment of a multivector.
    """

    def __init__(self, algebra, features, init: float = 0):
        super().__init__()
        self.algebra = algebra
        self.in_features = features
        max_seq = 3000 # used to cap the parameters (Note: this is not the best approach)

        # This parameter that learn how much to scale the input data
        # in particular the how much scale the norm of input (see forward)
        self.a = nn.Parameter(torch.zeros(max_seq, algebra.n_subspaces) + init)


    def forward(self, input):
        # Small change to take in account batch size extra dimention
        assert input.shape[2] == self.in_features #
        # print(f"input.shape => {input.shape}")

        norms = torch.cat(self.algebra.norms(input), dim=-1)
        # print(f"norms.shape  before => {norms.shape}")
        s_a = torch.sigmoid(self.a)
        # print(f"s_a.shape => {s_a.shape}")
        norms = s_a[:input.shape[1], :] * (norms - 1) + 1  # interpolates between 1 and the norm
        # print(f"norms.shape  after => {norms.shape}")

        # When you see repeat_interleave usually means that
        # the same thing is repeated for each subspace
        norms = norms.repeat_interleave(self.algebra.subspaces, dim=-1)
        # print(f"norms.shape  after repeat interleave=> {norms.shape}")
        normalized = input / (norms + 1e-6)
        return normalized
    

class FullyConnectedSteerableGeometricProductLayer(nn.Module):
    def __init__(self, algebra, features):
        """
        Fully connected steerable geometric product layer: a nn Module used to compute pairwise geometric products between multivectors of a same input sequence.

        Args:
            agebra: Geometric algebra object
            features: The number of features for the geometric product layer
        """
        super().__init__()
        self.algebra = algebra
        self.features = features

        self.normalization = NormalizationLayer(algebra, features) # to change
        self.q_prj = MVLinear(algebra, 2048, 2048)
        self.k_prj = MVLinear(algebra, 2048, 2048)

    # @torch.jit.script
    def forward(self, input):
        batch, seq, dim = input.shape

        # print(f"Input shape: {input.shape}")

        # mv projection
        q = self.q_prj(input)
        k = self.k_prj(input)

        # mv normalization
        q = self.normalization(q)
        k = self.normalization(k)

        # Dimention adjustments
        cayley = self.algebra.cayley.to(input.device) # [dim, dim, dim]
        q_einsum = q.unsqueeze(2)  # [batch, seq, 1, dim]
        k_einsum = k.unsqueeze(1)  # [batch, 1, seq, dim]

        # Make tensor contigous in memory for performance optimization
        q_einsum = q_einsum.contiguous()
        k_einsum = k_einsum.contiguous()
        cayley = cayley.contiguous()

        # Half precision for performance optimization
        q_einsum = q_einsum.half()
        k_einsum = k_einsum.half()
        cayley = cayley.half()

        # Serve as context managers or decorators that allow regions
        # of the script to run in mixed precision
        with torch.amp.autocast('cuda'):
            output = fast_einsum(q_einsum, cayley, k_einsum)

        """
        # comment the previous 2 line and uncomment this to monitor time on gpu
        with torch.profiler.profile(
            activities=[
                torch.profiler.ProfilerActivity.CPU,
                torch.profiler.ProfilerActivity.CUDA,
            ],
            record_shapes=True
        ) as prof:
            with torch.amp.autocast('cuda'):
                output = fast_einsum(q_einsum, cayley, k_einsum)
                output = torch.einsum("...i,ijk,...k->...j", q_einsum, cayley, k_einsum)
        print(prof.key_averages().table(sort_by="cuda_time_total"))
        """

        # print(f"Attention output: {output.shape}")

        return output



class GeometricProductAttention(nn.Module):
    def __init__(self, algebra, embed_dim):
        """
        Self-Attention layer using geometric algebra operation.

        Args:
            algebra: Geometric algebra object
            features: The number of features for the geometric product layer
        """
        super(GeometricProductAttention, self).__init__()

        self.algebra = algebra
        self.subspaces_dims = algebra.subspaces
        self.gp_layer = FullyConnectedSteerableGeometricProductLayer(algebra, features=embed_dim)

        # Single projection layer to learn common propertires
        self.att_prj = nn.Linear(embed_dim, 1)
        self.dropout = nn.Dropout(p=0.5)

    def forward(self, x):
        # Compute pairwise geometric products using the geometric product layer
        # start = time.time()
        new_mv = self.gp_layer(x)

        # apply attention score projection
        output = self.att_prj(new_mv.float())

        # end = time.time()
        # print(f"attention score computation in {end - start:.4f} seconds") # attention operation time

        return output


class SelfAttentionGA(nn.Module):
    def __init__(self, algebra, embed_dim):
        super(SelfAttentionGA, self).__init__()

        self.algebra = algebra
        self.v_proj = nn.Linear(2**algebra.dim, 112)
        # self.v_proj = MVLinear(algebra, 2048, embed_dim)
        self.ga_attention = GeometricProductAttention(algebra, embed_dim)

    def forward(self, x):
        x = self.algebra.embed_grade(x, 1) # shape: [B, P, 8]
        # print(f"MV embedding: {x.shape}")
        batch_size, seq_length, embed_dim = x.size() 
        v = self.v_proj(x) 
        # print(f"Value matrix shape: {v.shape}")

        # Compute attention scores using geometric product
        mv_attn = self.ga_attention(x).squeeze(-1)
        # print(f"attention scores: {mv_attn.shape}")

        attn_probs = torch.softmax(mv_attn, dim=-1)
        # print(f"attention probs: {attn_probs.shape}")

        # Apply attention to values tensor
        return torch.einsum("bqk,bvd->bqd", attn_probs, v)
