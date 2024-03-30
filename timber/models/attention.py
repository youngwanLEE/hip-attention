import os
import torch

from timber.models.timber_attention.attention1_gpu import flash_attention
from timber.models.timber_attention.attention1_block_gpu import timber_attention
from timber.models.attn_l1_loss import compute_attn_lp_loss_triton


def custom_attention(
        query_states, key_states, value_states,
        attention_mask, causal_mask,
        attention_dropout,

        # Attention method
        attention_method='timber',  # 'none', 'reformer', 'performer', 'timber'
        tree_reformer=None, tree_performer=None,

        # Timber parameters
        tree_k=512, tree_block_size_q=32, tree_block_size_k=2,
        tree_dense_queries=0, tree_last_dense_queries=0,
        tree_sampling_method='first',

        # Latency optimization tweaks
        tree_enable_flash=False, tree_enable_sparq=False, tree_use_sliding_window=False,

        # Context averaging parameters
        tree_using_context_avg=False, tree_avgpool_scaler=None, last_cumsum=None, hidden_states=None,

        # RoPE parameters
        tree_rope_method='none', rope_cos=None, rope_sin=None, position_ids=None,

        # Attention sparsity loss
        output_attn_sparsity_loss=False, tree_lp_norm_coeff=0.5,
):
    """
    @param query_states: (N, H, TDST, HID)
    @param key_states: (N, H, TSRC, HID)
    @param value_states: (N, H, TSRC, HID)
    @param attention_mask: (N, 1, TDST, TSRC)
    @param causal_mask: (1, 1, TDST, TSRC)
    @param attention_dropout: Dropout probability
    @param attention_method: Attention method: ['none', 'reformer', 'performer', 'timber']
    @param tree_reformer: Optional. Reformer object
    @param tree_performer: Optional. Performer object
    @param tree_k: Number of tokens to attend to for each query token in Timber attention
    @param tree_block_size_q: Query block size for Timber attention
    @param tree_block_size_k: Key block size for Timber attention
    @param tree_dense_queries: Number of dense queries
    @param tree_last_dense_queries: Number of last dense queries
    @param tree_sampling_method: Sampling method for Timber attention: ['first', 'random']
    @param tree_enable_flash: Enable flash attention
    @param tree_enable_sparq: Enable SparQ attention
    @param tree_use_sliding_window: Use sliding window for Timber attention
    @param tree_using_context_avg: Use context averaging for Timber attention
    @param tree_avgpool_scaler: Average pooling scaler
    @param last_cumsum: Last cumsum for context averaging
    @param hidden_states: Hidden states for context averaging
    @param tree_rope_method: RoPE method: ['none', 'self_extend']
    @param rope_cos: Used in self-extend RoPE method
    @param rope_sin: Used in self-extend RoPE method
    @param position_ids: Position IDs for self-extend RoPE method
    @param output_attn_sparsity_loss: Whether to compute attention sparsity regularization
    @param tree_lp_norm_coeff: Lp norm coefficient for attention sparsity regularization
    @return: Attention output, last cumsum, attention sparsity loss
    """
    attn_sparsity_loss = None

    if attention_method == 'none':
        # SDPA with memory-efficient backend is currently (torch==2.1.2) bugged with non-contiguous inputs with custom attn_mask,
        # Reference: https://github.com/pytorch/pytorch/issues/112577.
        if query_states.device.type == "cuda" and attention_mask is not None:
            query_states = query_states.contiguous()
            key_states = key_states.contiguous()
            value_states = value_states.contiguous()

        attn_output = torch.nn.functional.scaled_dot_product_attention(
            query_states,
            key_states,
            value_states,
            attn_mask=causal_mask,
            dropout_p=attention_dropout,
        )

        if os.environ.get('CHECKOUT_STATES', '0') == '1':
            os.makedirs('./cache/llama/', exist_ok=True)
            torch.save({
                'q': query_states,
                'k': key_states,
                'v': value_states,
                'out': attn_output,
            }, './cache/llama/qkvout.pth')
            input('stored. press enter to continue >>> ')

    elif attention_method == 'reformer':
        q = query_states  # / (query_states.shape[-1] ** 0.5)
        k = key_states
        v = value_states

        N, H, TDST, HID = q.shape
        _, _, TSRC, _ = k.shape
        assert k.shape == v.shape

        q = q.reshape(N * H, TDST, HID)  # .contiguous()
        # k = k.reshape(N*H, TSRC, HID) #.contiguous()
        v = v.reshape(N * H, TSRC, HID)  # .contiguous()

        tree_reformer.bucket_size = tree_k

        attn_output, attn, buckets = tree_reformer(q, v)  # (10, 1024, 128)
        attn_output = attn_output.view(N, H, TDST, HID)  # .to(hidden_states.dtype)

    elif attention_method == 'performer':
        q = query_states  # / (query_states.shape[-1] ** 0.5)
        k = key_states
        v = value_states

        with torch.autocast('cuda', enabled=False):
            attn_output = tree_performer(q.to(torch.float32), k.to(torch.float32), v.to(torch.float32))
        attn_output = attn_output.to(q.dtype)

    elif attention_method == 'timber':
        q = query_states / (query_states.shape[-1] ** 0.5)
        k = key_states
        v = value_states

        N, H, TDST, HID = q.shape
        _, _, TSRC, _ = k.shape
        assert k.shape == v.shape

        # For L1 loss of attention map
        if output_attn_sparsity_loss:
            # select random `select_n` queries for speedup
            select_n = 1024
            selection = torch.randperm(TDST, device=q.device)[:select_n]
            attn_sparsity_loss = compute_attn_lp_loss_triton(
                q[..., selection, :], k,
                p=tree_lp_norm_coeff,
                attend_lengths=selection.expand(N, select_n)
            ).mean(-1)

        q = q.reshape(N * H, TDST, HID)  # .contiguous()
        k = k.reshape(N * H, TSRC, HID)  # .contiguous()
        v = v.reshape(N * H, TSRC, HID)  # .contiguous()

        LAST_DENSE_QUERIES = tree_last_dense_queries

        if LAST_DENSE_QUERIES == 0:
            LAST_DENSE_QUERIES = None
        if isinstance(LAST_DENSE_QUERIES, int):
            assert LAST_DENSE_QUERIES < 0
        else:
            assert LAST_DENSE_QUERIES == None

        current_query_index = TSRC - TDST
        attn_outputs = []

        q_timber = q[:, :LAST_DENSE_QUERIES, :]
        try:
            attn_output_timber, _ = timber_attention(
                q_timber,
                k[:, :LAST_DENSE_QUERIES, :],
                v[:, :LAST_DENSE_QUERIES, :],
                mask_k=tree_k,
                block_size_q=tree_block_size_q,
                block_size_k=tree_block_size_k,
                dense_queries_exp=tree_dense_queries,
                rope_method=tree_rope_method,
                rope_cos=rope_cos.squeeze(0) if rope_cos is not None else None,
                rope_sin=rope_sin.squeeze(0) if rope_sin is not None else None,
                position_ids=position_ids,
                enable_sparq=tree_enable_sparq,
                is_flash=tree_enable_flash,
                using_sliding_window=tree_use_sliding_window,
                sampling_method=tree_sampling_method,
            )
        except RuntimeError as ex:
            os.makedirs('cache/timber', exist_ok=True)
            torch.save({
                'q': q_timber,
                'k': k[:, :LAST_DENSE_QUERIES, :],
                'v': v[:, :LAST_DENSE_QUERIES, :],
                'mask_k': tree_k,
                'block_size_q': tree_block_size_q,
                'block_size_k': tree_block_size_k,
            }, 'cache/timber/qkv.pth')
            raise Exception('oops timber is dead, check cache/timber/qkv.pth') from ex

        # NOTE: accumulation should be done with fp32
        if tree_using_context_avg:

            if last_cumsum is None:
                last_cumsum = v.cumsum(-2, dtype=torch.float32)
                last_cumsum = last_cumsum[:, TSRC - TDST:LAST_DENSE_QUERIES, :]
            else:
                last_cumsum = last_cumsum.flatten(0, 1)
                curr_v = v[:, -q_timber.shape[-2]:LAST_DENSE_QUERIES, :]
                curr_v = curr_v.cumsum(-2, dtype=torch.float32)
                last_cumsum = curr_v + last_cumsum[:, -1:, :]

            context_avg = last_cumsum / torch.arange(
                current_query_index + 1,
                current_query_index + 1 + q_timber.shape[1],
                device=v.device
            )[None, :, None]
            context_avg = context_avg.to(v.dtype)

            last_cumsum = last_cumsum.unflatten(0, (N, H))

            # N, H, TDST
            scale_avg = torch.sigmoid(
                tree_avgpool_scaler(hidden_states[:, :LAST_DENSE_QUERIES, :]).transpose(-1, -2).reshape(N * H, -1, 1)
            ) * 0.25 * torch.clamp(1.0 - (tree_k / torch.arange(TSRC - TDST, TSRC - TDST + q_timber.shape[1], device=v.device)), 0.0, 1.0)[None, :, None].to(v.dtype)
            # NOTE: 0.25 is just heuristic
            # NOTE: 256 is top-k value
            attn_output_timber = (attn_output_timber * (1 - scale_avg) + context_avg * scale_avg).to(v.dtype)
        attn_outputs.append(attn_output_timber)

        if LAST_DENSE_QUERIES is not None:
            flash_attention_mask = torch.zeros((N * H, abs(LAST_DENSE_QUERIES), TSRC), dtype=q.dtype,
                                               device=q.device)
            attn_output_last_flash, _ = flash_attention(
                q[:, LAST_DENSE_QUERIES:, :],
                k[:, :, :],
                v[:, :, :],
                flash_attention_mask,
            )
            attn_outputs.append(attn_output_last_flash)

        if len(attn_outputs) > 1:
            attn_output = torch.cat(attn_outputs, dim=-2)
        else:
            attn_output = attn_outputs[0]

        attn_output = attn_output.view(N, H, TDST, HID)  # .to(hidden_states.dtype)

    else:
        raise Exception(attention_method)

    return attn_output, last_cumsum, attn_sparsity_loss
