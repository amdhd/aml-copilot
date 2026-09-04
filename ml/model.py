"""Two-layer GAT over the transaction graph. Binary node classification."""

import torch.nn.functional as F
from torch import nn
from torch_geometric.nn import GATConv


class GAT(nn.Module):
    def __init__(self, in_dim: int, hidden: int = 64, heads: int = 4, dropout: float = 0.3):
        super().__init__()
        self.dropout = dropout
        self.conv1 = GATConv(in_dim, hidden, heads=heads, dropout=dropout)
        self.conv2 = GATConv(hidden * heads, hidden, heads=1, dropout=dropout)
        self.out = nn.Linear(hidden, 1)

    def forward(self, x, edge_index):
        x = F.dropout(x, self.dropout, self.training)
        x = F.elu(self.conv1(x, edge_index))
        x = F.dropout(x, self.dropout, self.training)
        x = F.elu(self.conv2(x, edge_index))
        return self.out(x).squeeze(-1)

    def forward_with_attention(self, x, edge_index):
        """Second call path: same logits, plus which neighbouring transactions
        layer 1 attended to. Eval only -- dropout must be off to match forward."""
        h, (att_index, alpha) = self.conv1(x, edge_index, return_attention_weights=True)
        h = F.elu(self.conv2(F.elu(h), edge_index))
        return self.out(h).squeeze(-1), att_index, alpha.mean(dim=1)
