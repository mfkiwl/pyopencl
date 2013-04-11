"""Scan primitive."""

from __future__ import division

__copyright__ = """
Copyright 2011-2012 Andreas Kloeckner
Copyright 2008-2011 NVIDIA Corporation
"""

__license__ = """
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Derived from thrust/detail/backend/cuda/detail/fast_scan.inl
within the Thrust project, https://code.google.com/p/thrust/

Direct browse link:
https://code.google.com/p/thrust/source/browse/thrust/detail/backend/cuda/detail/fast_scan.inl
"""



import numpy as np

import pyopencl as cl
import pyopencl.array
from pyopencl.tools import (dtype_to_ctype, bitlog2,
        KernelTemplateBase, _process_code_for_macro,
        get_arg_list_scalar_arg_dtypes)
import pyopencl._mymako as mako
from pyopencl._cluda import CLUDA_PREAMBLE





# {{{ preamble

SHARED_PREAMBLE = CLUDA_PREAMBLE + """//CL//
#define WG_SIZE ${wg_size}

#define SCAN_EXPR(a, b, across_seg_boundary) ${scan_expr}
#define INPUT_EXPR(i) (${input_expr})
%if is_segmented:
    #define IS_SEG_START(i, a) (${is_segment_start_expr})
%endif

${preamble}

typedef ${dtype_to_ctype(scan_dtype)} scan_type;
typedef ${dtype_to_ctype(index_dtype)} index_type;

// NO_SEG_BOUNDARY is the largest representable integer in index_type.
// This assumption is used in code below.
#define NO_SEG_BOUNDARY ${str(np.iinfo(index_dtype).max)}
"""

# }}}

# {{{ main scan code

# Algorithm: Each work group is responsible for one contiguous
# 'interval'. There are just enough intervals to fill all compute
# units.  Intervals are split into 'units'. A unit is what gets
# worked on in parallel by one work group.
#
# in index space:
# interval > unit > local-parallel > k-group
#
# (Note that there is also a transpose in here: The data is read
# with local ids along linear index order.)
#
# Each unit has two axes--the local-id axis and the k axis.
#
# unit 0:
# | | | | | | | | | | ----> lid
# | | | | | | | | | |
# | | | | | | | | | |
# | | | | | | | | | |
# | | | | | | | | | |
#
# |
# v k (fastest-moving in linear index)
#
# unit 1:
# | | | | | | | | | | ----> lid
# | | | | | | | | | |
# | | | | | | | | | |
# | | | | | | | | | |
# | | | | | | | | | |
#
# |
# v k (fastest-moving in linear index)
#
# ...
#
# At a device-global level, this is a three-phase algorithm, in
# which first each interval does its local scan, then a scan
# across intervals exchanges data globally, and the final update
# adds the exchanged sums to each interval.
#
# Exclusive scan is realized by allowing look-behind (access to the
# preceding item) in the final update, by means of a local shift.
#
# NOTE: All segment_start_in_X indices are relative to the start
# of the array.

SCAN_INTERVALS_SOURCE = SHARED_PREAMBLE + r"""//CL//

#define K ${k_group_size}

// #define DEBUG
#ifdef DEBUG
    #define pycl_printf(ARGS) printf ARGS
#else
    #define pycl_printf(ARGS) /* */
#endif

KERNEL
REQD_WG_SIZE(WG_SIZE, 1, 1)
void ${name_prefix}_scan_intervals(
    ${argument_signature},
    GLOBAL_MEM scan_type *restrict partial_scan_buffer,
    const index_type N,
    const index_type interval_size
    %if is_first_level:
        , GLOBAL_MEM scan_type *restrict interval_results
    %endif
    %if is_segmented and is_first_level:
        // NO_SEG_BOUNDARY if no segment boundary in interval.
        , GLOBAL_MEM index_type *restrict g_first_segment_start_in_interval
    %endif
    %if store_segment_start_flags:
        , GLOBAL_MEM char *restrict g_segment_start_flags
    %endif
    )
{
    // index K in first dimension used for carry storage
    %if scan_dtype.itemsize > 4 and scan_dtype.itemsize % 8 == 0 and is_gpu:
        // Avoid bank conflicts by adding a single 32-bit value to the size of
        // the scan type.
        struct __attribute__ ((__packed__)) wrapped_scan_type
        {
            scan_type value;
            int dummy;
        };
        LOCAL_MEM struct wrapped_scan_type ldata[K + 1][WG_SIZE + 1];
    %else:
        struct wrapped_scan_type
        {
            scan_type value;
        };

        // padded in WG_SIZE to avoid bank conflicts
        LOCAL_MEM struct wrapped_scan_type ldata[K + 1][WG_SIZE];
    %endif

    %if is_segmented:
        LOCAL_MEM char l_segment_start_flags[K][WG_SIZE];
        LOCAL_MEM index_type l_first_segment_start_in_subtree[WG_SIZE];

        // only relevant/populated for local id 0
        index_type first_segment_start_in_interval = NO_SEG_BOUNDARY;

        index_type first_segment_start_in_k_group, first_segment_start_in_subtree;
    %endif

    // {{{ declare local data for input_fetch_exprs if any of them are stenciled

    <%
        fetch_expr_offsets = {}
        for name, arg_name, ife_offset in input_fetch_exprs:
            fetch_expr_offsets.setdefault(arg_name, set()).add(ife_offset)

        local_fetch_expr_args = set(
            arg_name
            for arg_name, ife_offsets in fetch_expr_offsets.items()
            if -1 in ife_offsets or len(ife_offsets) > 1)
    %>

    %for arg_name in local_fetch_expr_args:
        LOCAL_MEM ${arg_ctypes[arg_name]} l_${arg_name}[WG_SIZE*K];
    %endfor

    // }}}

    const index_type interval_begin = interval_size * GID_0;
    const index_type interval_end   = min(interval_begin + interval_size, N);

    const index_type unit_size  = K * WG_SIZE;

    index_type unit_base = interval_begin;

    %for is_tail in [False, True]:

        %if not is_tail:
            for(; unit_base + unit_size <= interval_end; unit_base += unit_size)
        %else:
            if (unit_base < interval_end)
        %endif

        {

            // {{{ carry out input_fetch_exprs
            // (if there are ones that need to be fetched into local)

            %if local_fetch_expr_args:
                for(index_type k = 0; k < K; k++)
                {
                    const index_type offset = k*WG_SIZE + LID_0;
                    const index_type read_i = unit_base + offset;

                    %for arg_name in local_fetch_expr_args:
                        %if is_tail:
                        if (read_i < interval_end)
                        %endif
                        {
                            l_${arg_name}[offset] = ${arg_name}[read_i];
                        }
                    %endfor
                }

                local_barrier();
            %endif

            pycl_printf(("after input_fetch_exprs\n"));

            // }}}

            // {{{ read a unit's worth of data from global

            for(index_type k = 0; k < K; k++)
            {
                const index_type offset = k*WG_SIZE + LID_0;
                const index_type read_i = unit_base + offset;

                %if is_tail:
                if (read_i < interval_end)
                %endif
                {
                    %for name, arg_name, ife_offset in input_fetch_exprs:
                        ${arg_ctypes[arg_name]} ${name};

                        %if arg_name in local_fetch_expr_args:
                            if (offset + ${ife_offset} >= 0)
                                ${name} = l_${arg_name}[offset + ${ife_offset}];
                            else if (read_i + ${ife_offset} >= 0)
                                ${name} = ${arg_name}[read_i + ${ife_offset}];
                            /*
                            else
                                if out of bounds, name is left undefined */

                        %else:
                            // ${arg_name} gets fetched directly from global
                            ${name} = ${arg_name}[read_i];

                        %endif
                    %endfor

                    scan_type scan_value = INPUT_EXPR(read_i);

                    const index_type o_mod_k = offset % K;
                    const index_type o_div_k = offset / K;
                    ldata[o_mod_k][o_div_k].value = scan_value;

                    %if is_segmented:
                        bool is_seg_start = IS_SEG_START(read_i, scan_value);
                        l_segment_start_flags[o_mod_k][o_div_k] = is_seg_start;
                    %endif
                    %if store_segment_start_flags:
                        g_segment_start_flags[read_i] = is_seg_start;
                    %endif
                }
            }

            pycl_printf(("after read from global\n"));

            // }}}

            // {{{ carry in from previous unit, if applicable

            %if is_segmented:
                local_barrier();

                first_segment_start_in_k_group = NO_SEG_BOUNDARY;
                if (l_segment_start_flags[0][LID_0])
                    first_segment_start_in_k_group = unit_base + K*LID_0;
            %endif

            if (LID_0 == 0 && unit_base != interval_begin)
            {
                ldata[0][0].value = SCAN_EXPR(ldata[K][WG_SIZE - 1].value, ldata[0][0].value,
                    %if is_segmented:
                        (l_segment_start_flags[0][0])
                    %else:
                        false
                    %endif
                    );
            }

            pycl_printf(("after carry-in\n"));

            // }}}

            local_barrier();

            // {{{ scan along k (sequentially in each work item)

            scan_type sum = ldata[0][LID_0].value;

            %if is_tail:
                const index_type offset_end = interval_end - unit_base;
            %endif

            for(index_type k = 1; k < K; k++)
            {
                %if is_tail:
                if (K * LID_0 + k < offset_end)
                %endif
                {
                    scan_type tmp = ldata[k][LID_0].value;
                    index_type seq_i = unit_base + K*LID_0 + k;

                    %if is_segmented:
                    if (l_segment_start_flags[k][LID_0])
                    {
                        first_segment_start_in_k_group = min(
                            first_segment_start_in_k_group,
                            seq_i);
                    }
                    %endif

                    sum = SCAN_EXPR(sum, tmp,
                        %if is_segmented:
                            (l_segment_start_flags[k][LID_0])
                        %else:
                            false
                        %endif
                        );

                    ldata[k][LID_0].value = sum;
                }
            }

            pycl_printf(("after scan along k\n"));

            // }}}

            // store carry in out-of-bounds (padding) array entry (index K) in the K direction
            ldata[K][LID_0].value = sum;

            %if is_segmented:
                l_first_segment_start_in_subtree[LID_0] = first_segment_start_in_k_group;
            %endif

            local_barrier();

            // {{{ tree-based local parallel scan

            // This tree-based scan works as follows:
            // - Each work item adds the previous item to its current state
            // - barrier
            // - Each work item adds in the item from two positions to the left
            // - barrier
            // - Each work item adds in the item from four positions to the left
            // ...
            // At the end, each item has summed all prior items.

            // across k groups, along local id
            // (uses out-of-bounds k=K array entry for storage)

            scan_type val = ldata[K][LID_0].value;

            <% scan_offset = 1 %>

            % while scan_offset <= wg_size:
                // {{{ reads from local allowed, writes to local not allowed

                if (LID_0 >= ${scan_offset})
                {
                    scan_type tmp = ldata[K][LID_0 - ${scan_offset}].value;
                    % if is_tail:
                    if (K*LID_0 < offset_end)
                    % endif
                    {
                        val = SCAN_EXPR(tmp, val,
                            %if is_segmented:
                                (l_first_segment_start_in_subtree[LID_0] != NO_SEG_BOUNDARY)
                            %else:
                                false
                            %endif
                            );
                    }

                    %if is_segmented:
                        // Prepare for l_first_segment_start_in_subtree, below.

                        // Note that this update must take place *even* if we're
                        // out of bounds.

                        first_segment_start_in_subtree = min(
                            l_first_segment_start_in_subtree[LID_0],
                            l_first_segment_start_in_subtree[LID_0 - ${scan_offset}]);
                    %endif
                }
                %if is_segmented:
                    else
                    {
                        first_segment_start_in_subtree =
                            l_first_segment_start_in_subtree[LID_0];
                    }
                %endif

                // }}}

                local_barrier();

                // {{{ writes to local allowed, reads from local not allowed

                ldata[K][LID_0].value = val;
                %if is_segmented:
                    l_first_segment_start_in_subtree[LID_0] =
                        first_segment_start_in_subtree;
                %endif

                // }}}

                local_barrier();

                %if 0:
                if (LID_0 == 0)
                {
                    printf("${scan_offset}: ");
                    for (int i = 0; i < WG_SIZE; ++i)
                    {
                        if (l_first_segment_start_in_subtree[i] == NO_SEG_BOUNDARY)
                            printf("- ");
                        else
                            printf("%d ", l_first_segment_start_in_subtree[i]);
                    }
                    printf("\n");
                }
                %endif

                <% scan_offset *= 2 %>
            % endwhile

            pycl_printf(("after tree scan\n"));

            // }}}

            // {{{ update local values

            if (LID_0 > 0)
            {
                sum = ldata[K][LID_0 - 1].value;

                for(index_type k = 0; k < K; k++)
                {
                    %if is_tail:
                    if (K * LID_0 + k < offset_end)
                    %endif
                    {
                        scan_type tmp = ldata[k][LID_0].value;
                        ldata[k][LID_0].value = SCAN_EXPR(sum, tmp,
                            %if is_segmented:
                                (unit_base + K * LID_0 + k
                                    >= first_segment_start_in_k_group)
                            %else:
                                false
                            %endif
                            );
                    }
                }
            }

            %if is_segmented:
                if (LID_0 == 0)
                {
                    // update interval-wide first-seg variable from current unit
                    first_segment_start_in_interval = min(
                        first_segment_start_in_interval,
                        l_first_segment_start_in_subtree[WG_SIZE-1]);
                }
            %endif

            pycl_printf(("after local update\n"));

            // }}}

            local_barrier();

            // {{{ write data

            %if is_gpu:
            {
                // work hard with index math to achieve contiguous 32-bit stores
                __global int *dest = (__global int *) (partial_scan_buffer + unit_base);

                <%

                assert scan_dtype.itemsize % 4 == 0

                ints_per_wg = wg_size
                ints_to_store = scan_dtype.itemsize*wg_size*k_group_size // 4

                %>

                const index_type scan_types_per_int = ${scan_dtype.itemsize//4};

                %for store_base in range(0, ints_to_store, ints_per_wg):
                    <%

                    # Observe that ints_to_store is divisible by the work group size already,
                    # so we won't go out of bounds that way.
                    assert store_base + ints_per_wg <= ints_to_store

                    %>

                    %if is_tail:
                    if (${store_base} + LID_0 < scan_types_per_int*(interval_end - unit_base))
                    %endif
                    {
                        index_type linear_index = ${store_base} + LID_0;
                        index_type linear_scan_data_idx = linear_index / scan_types_per_int;
                        index_type remainder = linear_index - linear_scan_data_idx * scan_types_per_int;

                        __local int *src = (__local int *) &(
                            ldata[linear_scan_data_idx % K][linear_scan_data_idx / K].value);

                        dest[linear_index] = src[remainder];
                    }
                %endfor
            }
            %else:
            for (index_type k = 0; k < K; k++)
            {
                const index_type offset = k*WG_SIZE + LID_0;

                %if is_tail:
                if (unit_base + offset < interval_end)
                %endif
                {
                    pycl_printf(("write: %d\n", unit_base + offset));
                    partial_scan_buffer[unit_base + offset] =
                        ldata[offset % K][offset / K].value;
                }
            }
            %endif

            pycl_printf(("after write\n"));

            // }}}

            local_barrier();
        }

    % endfor

    // write interval sum
    %if is_first_level:
        if (LID_0 == 0)
        {
            interval_results[GID_0] = partial_scan_buffer[interval_end - 1];
            %if is_segmented:
                g_first_segment_start_in_interval[GID_0] = first_segment_start_in_interval;
            %endif
        }
    %endif
}
"""

# }}}

# {{{ update

UPDATE_SOURCE = SHARED_PREAMBLE + r"""//CL//

KERNEL
REQD_WG_SIZE(WG_SIZE, 1, 1)
void ${name_prefix}_final_update(
    ${argument_signature},
    const index_type N,
    const index_type interval_size,
    GLOBAL_MEM scan_type *restrict interval_results,
    GLOBAL_MEM scan_type *restrict partial_scan_buffer
    %if is_segmented:
        , GLOBAL_MEM index_type *restrict g_first_segment_start_in_interval
    %endif
    %if is_segmented and use_lookbehind_update:
        , GLOBAL_MEM char *restrict g_segment_start_flags
    %endif
    )
{
    %if use_lookbehind_update:
        LOCAL_MEM scan_type ldata[WG_SIZE];
    %endif
    %if is_segmented and use_lookbehind_update:
        LOCAL_MEM char l_segment_start_flags[WG_SIZE];
    %endif

    const index_type interval_begin = interval_size * GID_0;
    const index_type interval_end = min(interval_begin + interval_size, N);

    // carry from last interval
    scan_type carry = ${neutral};
    if (GID_0 != 0)
        carry = interval_results[GID_0 - 1];

    %if is_segmented:
        const index_type first_seg_start_in_interval =
            g_first_segment_start_in_interval[GID_0];
    %endif

    %if not is_segmented and 'last_item' in output_statement:
        scan_type last_item = interval_results[GDIM_0-1];
    %endif

    %if not use_lookbehind_update:
        // {{{ no look-behind ('prev_item' not in output_statement -> simpler)

        index_type update_i = interval_begin+LID_0;

        %if is_segmented:
            index_type seg_end = min(first_seg_start_in_interval, interval_end);
        %endif

        for(; update_i < interval_end; update_i += WG_SIZE)
        {
            scan_type partial_val = partial_scan_buffer[update_i];
            scan_type item = SCAN_EXPR(carry, partial_val,
                %if is_segmented:
                    (update_i >= seg_end)
                %else:
                    false
                %endif
                );
            index_type i = update_i;

            { ${output_statement}; }
        }

        // }}}
    %else:
        // {{{ allow look-behind ('prev_item' in output_statement -> complicated)

        // We are not allowed to branch across barriers at a granularity smaller
        // than the whole workgroup. Therefore, the for loop is group-global,
        // and there are lots of local ifs.

        index_type group_base = interval_begin;
        scan_type prev_item = carry; // (A)

        for(; group_base < interval_end; group_base += WG_SIZE)
        {
            index_type update_i = group_base+LID_0;

            // load a work group's worth of data
            if (update_i < interval_end)
            {
                scan_type tmp = partial_scan_buffer[update_i];

                tmp = SCAN_EXPR(carry, tmp,
                    %if is_segmented:
                        (update_i >= first_seg_start_in_interval)
                    %else:
                        false
                    %endif
                    );

                ldata[LID_0] = tmp;

                %if is_segmented:
                    l_segment_start_flags[LID_0] = g_segment_start_flags[update_i];
                %endif
            }

            local_barrier();

            // find prev_item
            if (LID_0 != 0)
                prev_item = ldata[LID_0 - 1];
            /*
            else
                prev_item = carry (see (A)) OR last tail (see (B));
            */

            if (update_i < interval_end)
            {
                %if is_segmented:
                    if (l_segment_start_flags[LID_0])
                        prev_item = ${neutral};
                %endif

                scan_type item = ldata[LID_0];
                index_type i = update_i;
                { ${output_statement}; }
            }

            if (LID_0 == 0)
                prev_item = ldata[WG_SIZE - 1]; // (B)

            local_barrier();
        }

        // }}}
    %endif
}
"""

# }}}

# {{{ driver

# {{{ helpers

def _round_down_to_power_of_2(val):
    result = 2**bitlog2(val)
    if result > val:
        result >>=1

    assert result <= val
    return result

_PREFIX_WORDS = set("""
        ldata partial_scan_buffer global scan_offset
        segment_start_in_k_group carry
        g_first_segment_start_in_interval IS_SEG_START tmp Z
        val l_first_segment_start_in_subtree unit_size
        index_type interval_begin interval_size offset_end K
        SCAN_EXPR do_update WG_SIZE
        first_segment_start_in_k_group scan_type
        segment_start_in_subtree offset interval_results interval_end
        first_segment_start_in_subtree unit_base
        first_segment_start_in_interval k INPUT_EXPR
        prev_group_sum prev pv value partial_val pgs
        is_seg_start update_i scan_item_at_i seq_i read_i
        l_ o_mod_k o_div_k l_segment_start_flags scan_value sum
        first_seg_start_in_interval g_segment_start_flags
        group_base seg_end my_val DEBUG ARGS
        ints_to_store ints_per_wg scan_types_per_int linear_index
        linear_scan_data_idx dest src store_base wrapped_scan_type
        dummy

        LID_2 LID_1 LID_0
        LDIM_0 LDIM_1 LDIM_2
        GDIM_0 GDIM_1 GDIM_2
        GID_0 GID_1 GID_2
        """.split())

_IGNORED_WORDS = set("""
        4 8 32

        typedef for endfor if void while endwhile endfor endif else const printf
        None return bool n char true false ifdef pycl_printf str range assert
        np iinfo max itemsize __packed__ struct restrict

        set iteritems len setdefault

        GLOBAL_MEM LOCAL_MEM_ARG WITHIN_KERNEL LOCAL_MEM KERNEL REQD_WG_SIZE
        local_barrier
        CLK_LOCAL_MEM_FENCE OPENCL EXTENSION
        pragma __attribute__ __global __kernel __local
        get_local_size get_local_id cl_khr_fp64 reqd_work_group_size
        get_num_groups barrier get_group_id

        _final_update _scan_intervals _debug_scan

        positions all padded integer its previous write based writes 0
        has local worth scan_expr to read cannot not X items False bank
        four beginning follows applicable item min each indices works side
        scanning right summed relative used id out index avoid current state
        boundary True across be This reads groups along Otherwise undetermined
        store of times prior s update first regardless Each number because
        array unit from segment conflicts two parallel 2 empty define direction
        CL padding work tree bounds values and adds
        scan is allowed thus it an as enable at in occur sequentially end no
        storage data 1 largest may representable uses entry Y meaningful
        computations interval At the left dimension know d
        A load B group perform shift tail see last OR
        this add fetched into are directly need
        gets them stenciled that undefined
        there up any ones or name only relevant populated
        even wide we Prepare int seg Note re below place take variable must
        intra Therefore find code assumption
        branch workgroup complicated granularity phase remainder than simpler
        We smaller look ifs lots self behind allow barriers whole loop
        after already Observe achieve contiguous stores hard go with by math
        size won t way divisible bit so Avoid declare adding single type

        is_tail is_first_level input_expr argument_signature preamble
        double_support neutral output_statement
        k_group_size name_prefix is_segmented index_dtype scan_dtype
        wg_size is_segment_start_expr fetch_expr_offsets
        arg_ctypes ife_offsets input_fetch_exprs def
        ife_offset arg_name local_fetch_expr_args update_body
        update_loop_lookbehind update_loop_plain update_loop
        use_lookbehind_update store_segment_start_flags
        update_loop first_seg scan_dtype dtype_to_ctype
        is_gpu

        a b prev_item i last_item prev_value
        N NO_SEG_BOUNDARY across_seg_boundary
        """.split())

def _make_template(s):
    leftovers = set()

    def replace_id(match):
        # avoid name clashes with user code by adding 'psc_' prefix to
        # identifiers.

        word = match.group(1)
        if word in _IGNORED_WORDS:
            return word
        elif word in _PREFIX_WORDS:
            return "psc_"+word
        else:
            leftovers.add(word)
            return word

    import re
    s = re.sub(r"\b([a-zA-Z0-9_]+)\b", replace_id, s)

    if leftovers:
        from warnings import warn
        warn("leftover words in identifier prefixing: " + " ".join(leftovers))

    return mako.template.Template(s, strict_undefined=True)

from pytools import Record
class _ScanKernelInfo(Record):
    pass

# }}}

class ScanPerformanceWarning(UserWarning):
    pass

class _GenericScanKernelBase(object):
    # {{{ constructor, argument processing

    def __init__(self, ctx, dtype,
            arguments, input_expr, scan_expr, neutral, output_statement,
            is_segment_start_expr=None, input_fetch_exprs=[],
            index_dtype=np.int32,
            name_prefix="scan", options=[], preamble="", devices=None):
        """
        :arg ctx: a :class:`pyopencl.Context` within which the code
            for this scan kernel will be generated.
        :arg dtype: the :class:`numpy.dtype` with which the scan will
            be performed. May be a structured type if that type was registered
            through :func:`pyopencl.tools.get_or_register_dtype`.
        :arg arguments: A string of comma-separated C argument declarations.
            If *arguments* is specified, then *input_expr* must also be
            specified. All types used here must be known to PyOpenCL.
            (see :func:`pyopencl.tools.get_or_register_dtype`).
        :arg scan_expr: The associative, binary operation carrying out the scan,
            represented as a C string. Its two arguments are available as `a`
            and `b` when it is evaluated. `b` is guaranteed to be the
            'element being updated', and `a` is the increment. Thus,
            if some data is supposed to just propagate along without being
            modified by the scan, it should live in `b`.

            This expression may call functions given in the *preamble*.

            Another value available to this expression is `across_seg_boundary`,
            a C `bool` indicating whether this scan update is crossing a
            segment boundary, as defined by `is_segment_start_expr`.
            The scan routine does not implement segmentation
            semantics on its own. It relies on `scan_expr` to do this.
            This value is available (but always `false`) even for a
            non-segmented scan.

            .. note::

                In early pre-releases of the segmented scan,
                segmentation semantics were implemented *without*
                relying on `scan_expr`.

        :arg input_expr: A C expression, encoded as a string, resulting
            in the values to which the scan is applied. This may be used
            to apply a mapping to values stored in *arguments* before being
            scanned. The result of this expression must match *dtype*.
            The index intended to be mapped is available as `i` in this
            expression. This expression may also use the variables defined
            by *input_fetch_expr*.

            This expression may also call functions given in the *preamble*.
        :arg output_statement: a C statement that writes
            the output of the scan. It has access to the scan result as `item`,
            the preceding scan result item as `prev_item`, and the current index
            as `i`. `prev_item` in a segmented scan will be the neutral element
            at a segment boundary, not the immediately preceding item.

            Using *prev_item* in output statement has a small run-time cost.
            `prev_item` enables the construction of an exclusive scan.

            For non-segmented scans, *output_statement* may also reference
            `last_item`, which evaluates to the scan result of the last
            array entry.
        :arg is_segment_start_expr: A C expression, encoded as a string,
            resulting in a C `bool` value that determines whether a new
            scan segments starts at index *i*.  If given, makes the scan a
            segmented scan. Has access to the current index `i`, the result
            of *input_expr* as a, and in addition may use *arguments* and
            *input_fetch_expr* variables just like *input_expr*.

            If it returns true, then previous sums will not spill over into the
            item with index *i* or subsequent items.
        :arg input_fetch_exprs: a list of tuples *(NAME, ARG_NAME, OFFSET)*.
            An entry here has the effect of doing the equivalent of the following
            before input_expr::

                ARG_NAME_TYPE NAME = ARG_NAME[i+OFFSET];

            `OFFSET` is allowed to be 0 or -1, and `ARG_NAME_TYPE` is the type
            of `ARG_NAME`.
        :arg preamble: |preamble|

        The first array in the argument list determines the size of the index
        space over which the scan is carried out, and thus the values over
        which the index *i* occurring in a number of code fragments in
        arguments above will vary.

        All code fragments further have access to N, the number of elements
        being processed in the scan.
        """

        self.context = ctx
        dtype = self.dtype = np.dtype(dtype)

        if neutral is None:
            from warnings import warn
            warn("not specifying 'neutral' is deprecated and will lead to "
                    "wrong results if your scan is not in-place or your "
                    "'output_statement' does something otherwise non-trivial",
                    stacklevel=2)

        if dtype.itemsize % 4 != 0:
            raise TypeError("scan value type must have size divisible by 4 bytes")

        self.index_dtype = np.dtype(index_dtype)
        if np.iinfo(self.index_dtype).min >= 0:
            raise TypeError("index_dtype must be signed")

        if devices is None:
            devices = ctx.devices
        self.devices = devices
        self.options = options

        from pyopencl.tools import parse_arg_list
        self.parsed_args = parse_arg_list(arguments)
        from pyopencl.tools import VectorArg
        self.first_array_idx = [
                i for i, arg in enumerate(self.parsed_args)
                if isinstance(arg, VectorArg)][0]

        self.input_expr = input_expr

        self.is_segment_start_expr = is_segment_start_expr
        self.is_segmented = is_segment_start_expr is not None
        if self.is_segmented:
            is_segment_start_expr = _process_code_for_macro(is_segment_start_expr)

        self.output_statement = output_statement

        for name, arg_name, ife_offset in input_fetch_exprs:
            if ife_offset not in [0, -1]:
                raise RuntimeError("input_fetch_expr offsets must either be 0 or -1")
        self.input_fetch_exprs = input_fetch_exprs

        arg_dtypes = {}
        arg_ctypes = {}
        for arg in self.parsed_args:
            arg_dtypes[arg.name] = arg.dtype
            arg_ctypes[arg.name] = dtype_to_ctype(arg.dtype)

        self.options = options
        self.name_prefix = name_prefix

        # {{{ set up shared code dict

        from pytools import all
        from pyopencl.characterize import has_double_support

        self.code_variables = dict(
            np=np,
            dtype_to_ctype=dtype_to_ctype,
            preamble=preamble,
            name_prefix=name_prefix,
            index_dtype=self.index_dtype,
            scan_dtype=dtype,
            is_segmented=self.is_segmented,
            arg_dtypes=arg_dtypes,
            arg_ctypes=arg_ctypes,
            scan_expr=_process_code_for_macro(scan_expr),
            neutral=_process_code_for_macro(neutral),
            is_gpu=self.devices[0].type == cl.device_type.GPU,
            double_support=all(
                has_double_support(dev) for dev in devices),
            )

        # }}}

        self.finish_setup()

    # }}}

class GenericScanKernel(_GenericScanKernelBase):
    """Generates and executes code that performs prefix sums ("scans") on
    arbitrary types, with many possible tweaks.

    Usage example::

        from pyopencl.scan import GenericScanKernel
        knl = GenericScanKernel(
                context, np.int32,
                arguments="__global int *ary",
                input_expr="ary[i]",
                scan_expr="a+b", neutral="0",
                output_statement="ary[i+1] = item;")

        a = cl.array.arange(queue, 10000, dtype=np.int32)
        scan_kernel(a, queue=queue)

    """

    def finish_setup(self):
        use_lookbehind_update = "prev_item" in self.output_statement
        self.store_segment_start_flags = self.is_segmented and use_lookbehind_update

        # {{{ find usable workgroup/k-group size, build first-level scan

        trip_count = 0

        avail_local_mem = min(
                dev.local_mem_size
                for dev in self.devices)

        if self.devices[0].type == cl.device_type.CPU:
            # (about the widest vector a CPU can support, also taking
            # into account that CPUs don't hide latency by large work groups
            max_scan_wg_size = 16
            wg_size_multiples = 4
        else:
            max_scan_wg_size = min(dev.max_work_group_size for dev in self.devices)
            wg_size_multiples = 64

        # k_group_size should be a power of two because of in-kernel
        # division by that number.

        solutions = []
        for k_exp in range(0, 9):
            for wg_size in range(wg_size_multiples, max_scan_wg_size+1,
                    wg_size_multiples):

                k_group_size = 2**k_exp
                lmem_use = self.get_local_mem_use(wg_size, k_group_size)
                if lmem_use + 256  <= avail_local_mem:
                    solutions.append((wg_size*k_group_size, k_group_size, wg_size))

        if self.devices[0].type == cl.device_type.GPU:
            from pytools import any
            for wg_size_floor in [256, 192, 128]:
                have_sol_above_floor = any(wg_size >= wg_size_floor
                        for _, _, wg_size in solutions)

                if have_sol_above_floor:
                    # delete all solutions not meeting the wg size floor
                    solutions = [(total, k_group_size, wg_size)
                            for total, k_group_size, wg_size in solutions
                            if wg_size >= wg_size_floor]
                    break

        _, k_group_size, max_scan_wg_size = max(solutions)

        while True:
            candidate_scan_info = self.build_scan_kernel(
                    max_scan_wg_size, self.parsed_args,
                    _process_code_for_macro(self.input_expr),
                    self.is_segment_start_expr,
                    input_fetch_exprs=self.input_fetch_exprs,
                    is_first_level=True,
                    store_segment_start_flags=self.store_segment_start_flags,
                    k_group_size=k_group_size)

            # Will this device actually let us execute this kernel
            # at the desired work group size? Building it is the
            # only way to find out.
            kernel_max_wg_size = min(
                    candidate_scan_info.kernel.get_work_group_info(
                        cl.kernel_work_group_info.WORK_GROUP_SIZE,
                        dev)
                    for dev in self.devices)

            if candidate_scan_info.wg_size <= kernel_max_wg_size:
                break
            else:
                max_scan_wg_size = min(kernel_max_wg_size, max_scan_wg_size)

            trip_count += 1
            assert trip_count <= 20

        self.first_level_scan_info = candidate_scan_info
        assert (_round_down_to_power_of_2(candidate_scan_info.wg_size)
                == candidate_scan_info.wg_size)

        # }}}

        # {{{ build second-level scan

        from pyopencl.tools import VectorArg
        second_level_arguments = self.parsed_args + [
                VectorArg(self.dtype, "interval_sums")]

        second_level_build_kwargs = {}
        if self.is_segmented:
            second_level_arguments.append(
                    VectorArg(self.index_dtype,
                        "g_first_segment_start_in_interval_input"))

            # is_segment_start_expr answers the question "should previous sums
            # spill over into this item". And since g_first_segment_start_in_interval_input
            # answers the question if a segment boundary was found in an interval of data,
            # then if not, it's ok to spill over.
            second_level_build_kwargs["is_segment_start_expr"] = \
                    "g_first_segment_start_in_interval_input[i] != NO_SEG_BOUNDARY"
        else:
            second_level_build_kwargs["is_segment_start_expr"] = None

        self.second_level_scan_info = self.build_scan_kernel(
                max_scan_wg_size,
                arguments=second_level_arguments,
                input_expr="interval_sums[i]",
                input_fetch_exprs=[],
                is_first_level=False,
                store_segment_start_flags=False,
                k_group_size=k_group_size,
                **second_level_build_kwargs)

        assert min(
                candidate_scan_info.kernel.get_work_group_info(
                    cl.kernel_work_group_info.WORK_GROUP_SIZE,
                    dev)
                for dev in self.devices) >= max_scan_wg_size

        # }}}

        # {{{ build final update kernel

        self.update_wg_size = min(max_scan_wg_size, 256)

        final_update_tpl = _make_template(UPDATE_SOURCE)
        final_update_src = str(final_update_tpl.render(
            wg_size=self.update_wg_size,
            output_statement=self.output_statement,
            argument_signature=", ".join(arg.declarator() for arg in self.parsed_args),
            is_segment_start_expr=self.is_segment_start_expr,
            input_expr=_process_code_for_macro(self.input_expr),
            use_lookbehind_update=use_lookbehind_update,
            **self.code_variables))

        final_update_prg = cl.Program(self.context, final_update_src).build(self.options)
        self.final_update_knl = getattr(
                final_update_prg,
                self.name_prefix+"_final_update")
        update_scalar_arg_dtypes = (
                get_arg_list_scalar_arg_dtypes(self.parsed_args)
                + [self.index_dtype, self.index_dtype, None, None])
        if self.is_segmented:
            update_scalar_arg_dtypes.append(None) # g_first_segment_start_in_interval
        if self.store_segment_start_flags:
            update_scalar_arg_dtypes.append(None) # g_segment_start_flags
        self.final_update_knl.set_scalar_arg_dtypes(update_scalar_arg_dtypes)

        # }}}

    # {{{ scan kernel build/properties

    def get_local_mem_use(self, k_group_size, wg_size):
        arg_dtypes = {}
        for arg in self.parsed_args:
            arg_dtypes[arg.name] = arg.dtype

        fetch_expr_offsets = {}
        for name, arg_name, ife_offset in self.input_fetch_exprs:
            fetch_expr_offsets.setdefault(arg_name, set()).add(ife_offset)

        return (
                # ldata
                self.dtype.itemsize*(k_group_size+1)*(wg_size+1)

                # l_segment_start_flags
                + k_group_size*wg_size

                # l_first_segment_start_in_subtree
                + self.index_dtype.itemsize*wg_size

                + k_group_size*wg_size*sum(
                    arg_dtypes[arg_name].itemsize
                    for arg_name, ife_offsets in fetch_expr_offsets.items()
                    if -1 in ife_offsets or len(ife_offsets) > 1))


    def build_scan_kernel(self, max_wg_size, arguments, input_expr,
            is_segment_start_expr, input_fetch_exprs, is_first_level,
            store_segment_start_flags, k_group_size):
        scalar_arg_dtypes = get_arg_list_scalar_arg_dtypes(arguments)

        # Empirically found on Nv hardware: no need to be bigger than this size
        wg_size = _round_down_to_power_of_2(
                min(max_wg_size, 256))

        scan_tpl = _make_template(SCAN_INTERVALS_SOURCE)
        scan_src = str(scan_tpl.render(
            wg_size=wg_size,
            input_expr=input_expr,
            k_group_size=k_group_size,
            argument_signature=", ".join(arg.declarator() for arg in arguments),
            is_segment_start_expr=is_segment_start_expr,
            input_fetch_exprs=input_fetch_exprs,
            is_first_level=is_first_level,
            store_segment_start_flags=store_segment_start_flags,
            **self.code_variables))

        prg = cl.Program(self.context, scan_src).build(self.options)

        knl = getattr(
                prg,
                self.code_variables["name_prefix"]+"_scan_intervals")

        scalar_arg_dtypes.extend(
                (None, self.index_dtype, self. index_dtype))
        if is_first_level:
            scalar_arg_dtypes.append(None) # interval_results
        if self.is_segmented and is_first_level:
            scalar_arg_dtypes.append(None) # g_first_segment_start_in_interval
        if store_segment_start_flags:
            scalar_arg_dtypes.append(None) # g_segment_start_flags
        knl.set_scalar_arg_dtypes(scalar_arg_dtypes)

        return _ScanKernelInfo(
                kernel=knl, wg_size=wg_size, knl=knl, k_group_size=k_group_size)

    # }}}

    def __call__(self, *args, **kwargs):
        # {{{ argument processing

        allocator = kwargs.get("allocator")
        queue = kwargs.get("queue")
        n = kwargs.get("size")

        if len(args) != len(self.parsed_args):
            raise TypeError("expected %d arguments, got %d" %
                    (len(self.parsed_args), len(args)))

        first_array = args[self.first_array_idx]
        allocator = allocator or first_array.allocator
        queue = queue or first_array.queue

        if n is None:
            n, = first_array.shape

        if n == 0:
            # We're done here.
            return

        data_args = []
        from pyopencl.tools import VectorArg
        for arg_descr, arg_val in zip(self.parsed_args, args):
            if isinstance(arg_descr, VectorArg):
                data_args.append(arg_val.data)
            else:
                data_args.append(arg_val)

        # }}}

        l1_info = self.first_level_scan_info
        l2_info = self.second_level_scan_info

        # see CL source above for terminology
        unit_size  = l1_info.wg_size * l1_info.k_group_size
        max_intervals = 3*max(dev.max_compute_units for dev in self.devices)

        from pytools import uniform_interval_splitting
        interval_size, num_intervals = uniform_interval_splitting(
                n, unit_size, max_intervals)

        # {{{ allocate some buffers

        interval_results = cl.array.empty(queue,
                num_intervals, dtype=self.dtype,
                allocator=allocator)

        partial_scan_buffer = cl.array.empty(
                queue, n, dtype=self.dtype,
                allocator=allocator)

        if self.store_segment_start_flags:
            segment_start_flags = cl.array.empty(
                    queue, n, dtype=np.bool,
                    allocator=allocator)

        # }}}

        # {{{ first level scan of interval (one interval per block)

        scan1_args = data_args + [
                partial_scan_buffer.data, n, interval_size, interval_results.data,
                ]

        if self.is_segmented:
            first_segment_start_in_interval = cl.array.empty(queue,
                    num_intervals, dtype=self.index_dtype,
                    allocator=allocator)
            scan1_args.append(first_segment_start_in_interval.data)

        if self.store_segment_start_flags:
            scan1_args.append(segment_start_flags.data)

        l1_info.kernel(
                queue, (num_intervals,), (l1_info.wg_size,),
                *scan1_args, **dict(g_times_l=True))

        # }}}

        # {{{ second level scan of per-interval results

        # can scan at most one interval
        assert interval_size >= num_intervals

        scan2_args = data_args + [
                interval_results.data, # interval_sums
                ]
        if self.is_segmented:
            scan2_args.append(first_segment_start_in_interval.data)
        scan2_args = scan2_args + [
                interval_results.data, # partial_scan_buffer
                num_intervals, interval_size]

        l2_info.kernel(
                queue, (1,), (l1_info.wg_size,),
                *scan2_args, **dict(g_times_l=True))

        # }}}

        # {{{ update intervals with result of interval scan

        upd_args = data_args + [
                n, interval_size, interval_results.data, partial_scan_buffer.data]
        if self.is_segmented:
            upd_args.append(first_segment_start_in_interval.data)
        if self.store_segment_start_flags:
            upd_args.append(segment_start_flags.data)

        self.final_update_knl(
                queue, (num_intervals,), (self.update_wg_size,),
                *upd_args, **dict(g_times_l=True))

        # }}}

# }}}

# {{{ debug kernel

DEBUG_SCAN_TEMPLATE = SHARED_PREAMBLE + r"""//CL//

KERNEL
REQD_WG_SIZE(1, 1, 1)
void ${name_prefix}_debug_scan(
    ${argument_signature},
    const index_type N)
{
    scan_type item = ${neutral};
    scan_type prev_item;

    for (index_type i = 0; i < N; ++i)
    {
        %for name, arg_name, ife_offset in input_fetch_exprs:
            ${arg_ctypes[arg_name]} ${name};
            %if ife_offset < 0:
                if (i+${ife_offset} >= 0)
                    ${name} = ${arg_name}[i+offset];
            %else:
                ${name} = ${arg_name}[i];
            %endif
        %endfor

        scan_type my_val = INPUT_EXPR(i);

        prev_item = item;
        %if is_segmented:
            bool is_seg_start = IS_SEG_START(i, my_val);
        %endif

        item = SCAN_EXPR(prev_item, my_val,
            %if is_segmented:
                is_seg_start
            %else:
                false
            %endif
            );

        {
            ${output_statement};
        }
    }
}
"""

class GenericDebugScanKernel(_GenericScanKernelBase):
    def finish_setup(self):
        scan_tpl = _make_template(DEBUG_SCAN_TEMPLATE)
        scan_src = str(scan_tpl.render(
            output_statement=self.output_statement,
            argument_signature=", ".join(arg.declarator() for arg in self.parsed_args),
            is_segment_start_expr=self.is_segment_start_expr,
            input_expr=_process_code_for_macro(self.input_expr),
            input_fetch_exprs=self.input_fetch_exprs,
            wg_size=1,
            **self.code_variables))

        scan_prg = cl.Program(self.context, scan_src).build(self.options)
        self.kernel = getattr(
                scan_prg, self.name_prefix+"_debug_scan")
        scalar_arg_dtypes = (
                get_arg_list_scalar_arg_dtypes(self.parsed_args)
                + [self.index_dtype])
        self.kernel.set_scalar_arg_dtypes(scalar_arg_dtypes)

    def __call__(self, *args, **kwargs):
        # {{{ argument processing

        allocator = kwargs.get("allocator")
        queue = kwargs.get("queue")
        n = kwargs.get("size")

        if len(args) != len(self.parsed_args):
            raise TypeError("expected %d arguments, got %d" %
                    (len(self.parsed_args), len(args)))

        first_array = args[self.first_array_idx]
        allocator = allocator or first_array.allocator
        queue = queue or first_array.queue

        if n is None:
            n, = first_array.shape

        data_args = []
        from pyopencl.tools import VectorArg
        for arg_descr, arg_val in zip(self.parsed_args, args):
            if isinstance(arg_descr, VectorArg):
                data_args.append(arg_val.data)
            else:
                data_args.append(arg_val)

        # }}}

        self.kernel(queue, (1,), (1,), *(data_args + [n]))

# }}}

# {{{ compatibility interface

class _LegacyScanKernelBase(GenericScanKernel):
    def __init__(self, ctx, dtype,
            scan_expr, neutral=None,
            name_prefix="scan", options=[], preamble="", devices=None):
        scan_ctype = dtype_to_ctype(dtype)
        GenericScanKernel.__init__(self,
                ctx, dtype,
                arguments="__global %s *input_ary, __global %s *output_ary" % (
                    scan_ctype, scan_ctype),
                input_expr="input_ary[i]",
                scan_expr=scan_expr,
                neutral=neutral,
                output_statement=self.ary_output_statement,
                options=options, preamble=preamble, devices=devices)

    def __call__(self, input_ary, output_ary=None, allocator=None, queue=None):
        allocator = allocator or input_ary.allocator
        queue = queue or input_ary.queue or output_ary.queue

        if output_ary is None:
            output_ary = input_ary

        if isinstance(output_ary, (str, unicode)) and output_ary == "new":
            output_ary = cl.array.empty_like(input_ary, allocator=allocator)

        if input_ary.shape != output_ary.shape:
            raise ValueError("input and output must have the same shape")

        if not input_ary.flags.forc:
            raise RuntimeError("ScanKernel cannot "
                    "deal with non-contiguous arrays")

        n, = input_ary.shape

        if not n:
            return output_ary

        GenericScanKernel.__call__(self,
                input_ary, output_ary, allocator=allocator, queue=queue)

        return output_ary

class InclusiveScanKernel(_LegacyScanKernelBase):
    ary_output_statement = "output_ary[i] = item;"

class ExclusiveScanKernel(_LegacyScanKernelBase):
    ary_output_statement = "output_ary[i] = prev_item;"

# }}}

# {{{ template

class ScanTemplate(KernelTemplateBase):
    def __init__(self,
            arguments, input_expr, scan_expr, neutral, output_statement,
            is_segment_start_expr=None, input_fetch_exprs=[],
            name_prefix="scan", preamble="", template_processor=None):

        KernelTemplateBase.__init__(self, template_processor=template_processor)
        self.arguments = arguments
        self.input_expr = input_expr
        self.scan_expr = scan_expr
        self.neutral = neutral
        self.output_statement = output_statement
        self.is_segment_start_expr = is_segment_start_expr
        self.input_fetch_exprs = input_fetch_exprs
        self.name_prefix = name_prefix
        self.preamble = preamble

    def build_inner(self, context, type_aliases=(), var_values=(),
            more_preamble="", more_arguments=(), declare_types=(),
            options=(), devices=None, scan_cls=GenericScanKernel):
        renderer = self.get_renderer(type_aliases, var_values, context, options)

        arg_list = renderer.render_argument_list(self.arguments, more_arguments)

        type_decl_preamble = renderer.get_type_decl_preamble(
                context.devices[0], declare_types, arg_list)

        return scan_cls(context, renderer.type_aliases["scan_t"],
            renderer.render_argument_list(self.arguments, more_arguments),
            renderer(self.input_expr), renderer(self.scan_expr), renderer(self.neutral),
            renderer(self.output_statement),
            is_segment_start_expr=renderer(self.is_segment_start_expr),
            input_fetch_exprs=self.input_fetch_exprs,
            index_dtype=renderer.type_aliases.get("index_t", np.int32),
            name_prefix=renderer(self.name_prefix), options=list(options),
            preamble=(
                type_decl_preamble
                + "\n"
                + renderer(self.preamble + "\n" + more_preamble)),
            devices=devices)

# }}}

# vim: filetype=pyopencl:fdm=marker
