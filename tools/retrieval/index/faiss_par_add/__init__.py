# coding=utf-8
# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Parallel version of Faiss's index.add()."""

import faiss
import numpy as np
import os
import torch

from tools.retrieval.data import load_data
from tools.retrieval.index.faiss_base import FaissBaseIndex
from tools.retrieval.index.index import Index
from tools.retrieval.utils import print_rank

class FaissParallelAddIndex(Index):

    def train(self, input_data_paths, dir_path, timer):
        # raise Exception("better to inherit from FaissBaseIndex?")
        index = FaissBaseIndex(self.args)
        return index.train(input_data_paths, dir_path, timer)

    @classmethod
    def get_num_rows(cls, num_batches):
        return int(np.ceil(np.log(num_batches) / np.log(2))) + 1
    @classmethod
    def get_num_cols(cls, num_batches, row):
        world_size = torch.distributed.get_world_size()
        return int(np.ceil(num_batches / world_size / 2**row))

    def get_partial_index_path_map(
            self,
            input_data_paths,
            dir_path,
            row,
            col,
            rank = None,
    ):

        rank = torch.distributed.get_rank() if rank is None else rank
        world_size = torch.distributed.get_world_size()
        num_batches = len(input_data_paths)

        batch_str_len = int(np.ceil(np.log(num_batches) / np.log(10))) + 1
        zf = lambda b : str(b).zfill(batch_str_len)

        # First batch id.
        batch_id_0 = 2**row * (rank + col * world_size)

        if batch_id_0 >= num_batches:
            return None

        # Other batch ids.
        if row == 0:
            batch_id_1 = batch_id_2 = batch_id_3 = batch_id_0
        else:
            batch_full_range = 2**row
            batch_half_range = int(batch_full_range / 2)
            batch_id_1 = batch_id_0 + batch_half_range - 1
            batch_id_2 = batch_id_1 + 1
            batch_id_3 = batch_id_2 + batch_half_range - 1

        # Batch ranges.
        def get_batch_range(b0, b1):
            if b1 >= num_batches:
                b1 = num_batches - 1
            if b0 >= num_batches:
                return None
            return b0, b1

        def batch_range_to_index_path(_row, _range):
            if _range is None:
                return None
            else:
                return os.path.join(
                    dir_path,
                    "added_%s_%s-%s.faissindex" % (
                        zf(_range[-1] - _range[0] + 1),
                        *[zf(b) for b in _range],
                    ),
                )

        input_batch_ranges = [
            get_batch_range(batch_id_0, batch_id_1),
            get_batch_range(batch_id_2, batch_id_3),
        ]
        output_batch_range = get_batch_range(batch_id_0, batch_id_3)

        # Path map.
        path_map = {
            "batch_id" : batch_id_0,
            "num_batches" : num_batches,
            "output_index_path" :
            batch_range_to_index_path(row, output_batch_range),
        }
        if row == 0:
            path_map["input_data_path"] = input_data_paths[batch_id_0]
        else:
            input_index_paths = \
                [batch_range_to_index_path(row-1, r) for r in input_batch_ranges]
            input_index_paths = [ p for p in input_index_paths if p is not None ]

            if not input_index_paths:
                return None

            path_map["input_index_paths"] = input_index_paths

        # Return.
        return path_map

    def get_missing_index_paths(
            self,
            input_data_paths,
            dir_path,
            timer,
            num_rows,
    ):

        world_size = torch.distributed.get_world_size()
        num_batches = len(input_data_paths)

        missing_index_paths = set()
        for row in range(num_rows - 1, -1, -1):

            num_cols = self.get_num_cols(num_batches, row)
            for col in range(num_cols):

                for rank in range(world_size):

                    # Input/output index paths.
                    path_map = self.get_partial_index_path_map(
                        input_data_paths,
                        dir_path,
                        row,
                        col,
                        rank,
                    )

                    # Handle edge cases.
                    if path_map is None:
                        continue

                    output_path = path_map["output_index_path"]
                    input_paths = path_map.get("input_index_paths", [])

                    if row == num_rows - 1 and not os.path.isfile(output_path):
                        missing_index_paths.add(output_path)

                    if output_path in missing_index_paths:
                        missing_input_paths = \
                            [ p for p in input_paths if not os.path.isfile(p) ]
                        missing_index_paths.update(missing_input_paths)

        return missing_index_paths

    def get_added_index_path(self, input_data_paths, dir_path):
        num_rows = self.get_num_rows(len(input_data_paths))
        index_path_map = self.get_partial_index_path_map(
            input_data_paths,
            dir_path,
            row = num_rows - 1,
            col = 0,
            rank = 0,
        )
        return index_path_map["output_index_path"]

    def encode_partial(self, partial_index_path_map, dir_path, timer):
        """Encode partial indexes (embarrassingly parallel).

        Encode the partial indexes, generally in batches of 1M vectors each.
        For each batch, the empty/trained index is loaded, and index.add() is
        called on each batch of data.
        """

        # Index & data paths.
        empty_index_path = self.get_empty_index_path(dir_path)
        partial_index_path = partial_index_path_map["output_index_path"]
        input_data_path = partial_index_path_map["input_data_path"]

        # If partial index exists, return.
        if os.path.isfile(partial_index_path):
            return

        # Load data batch.
        timer.push("load-data")
        input_data = load_data([input_data_path], timer)["data"].astype("f4")
        timer.pop()

        nvecs = len(input_data)
        print_rank("ivfpq / add / partial,  batch %d / %d. [ %d vecs ]" % (
            partial_index_path_map["batch_id"],
            partial_index_path_map["num_batches"],
            nvecs,
        ))

        timer.push("read")
        index = faiss.read_index(empty_index_path)
        # self.c_verbose(index, True) # with batch_size 1M ... too fast/verbose
        # self.c_verbose(index.quantizer, True)
        timer.pop()

        timer.push("add")
        index.add(input_data)
        timer.pop()

        timer.push("write")
        faiss.write_index(index, partial_index_path)
        timer.pop()

    def merge_partial(self, partial_index_path_map, dir_path, timer):
        '''Merge partial indexes.

        Pairwise merging
        '''

        # Extract inverted lists from full index.
        def get_invlists(index):
            return faiss.extract_index_ivf(index).invlists

        # Index paths.
        output_index_path = partial_index_path_map["output_index_path"]
        input_index_paths = partial_index_path_map["input_index_paths"]

        if not os.path.isfile(output_index_path):

            assert len(input_index_paths) >= 2, \
                "if singular input index, path should already exist."

            # Init output index.
            timer.push("read/init-output")
            output_index = faiss.read_index(input_index_paths[0])
            output_invlists = get_invlists(output_index)
            timer.pop()

            # Merge input indexes.
            for input_iter in range(1, len(input_index_paths)):

                input_index_path = input_index_paths[input_iter]
                assert input_index_path is not None, "missing input index."

                timer.push("read-input")
                input_index = faiss.read_index(input_index_path)
                input_invlists = get_invlists(input_index)
                timer.pop()

                print_rank("ivfpq / add / merge, input %d / %d. [ +%d -> %d ]" % (
                    input_iter,
                    len(input_index_paths),
                    input_index.ntotal,
                    input_index.ntotal + output_index.ntotal,
                ))

                timer.push("add")

                # Old way.
                # for list_id in range(input_invlists.nlist):
                #     output_invlists.add_entries(
                #         list_id,
                #         input_invlists.list_size(list_id),
                #         input_invlists.get_ids(list_id),
                #         input_invlists.get_codes(list_id),
                #     )

                # New way.
                output_invlists.merge_from(input_invlists, output_index.ntotal)

                timer.pop()

                output_index.ntotal += input_index.ntotal

            timer.push("write")
            faiss.write_index(output_index, output_index_path)
            timer.pop()

        # Delete input indexes.
        if len(input_index_paths) >= 2:
            timer.push("delete")
            for path in input_index_paths:
                os.remove(path)
            timer.pop()

    def add(self, input_data_paths, dir_path, timer):
        """Add vectors to index, in parallel.

        Two stage process:
        1. Encode partial indexes (i.e., starting with empty index, encode
           batches of 1M samples per partial index).
        2. Merge partial indexes.
           - This is a pairwise hierarchical merge.
           - We iterate log2(num_batches) 'rows' of merge.
           - Ranks move in lock-step across each row (i.e., 'cols')
        """

        # >>>
        faiss.omp_set_num_threads(4)
        # from lutil import pax
        # pax(0, {"nthreads": faiss.omp_get_max_threads()})
        # <<<

        # Num batches & rows.
        num_batches = len(input_data_paths)
        num_rows = self.get_num_rows(num_batches) # e.g., 47B -> ~15.52 rows

        # Missing index paths.
        missing_index_paths = self.get_missing_index_paths(
            input_data_paths,
            dir_path,
            timer,
            num_rows,
        )

        # Prevent race condition for missing paths. [ necessary? ]
        torch.distributed.barrier()

        # Iterate merge rows.
        for row in range(num_rows):

            timer.push("row-%d" % row)

            # Get number of columns for this merge row.
            num_cols = self.get_num_cols(num_batches, row)

            # Iterate merge columns.
            for col in range(num_cols):

                print_rank(0, "r %d / %d, c %d / %d." % (
                    row,
                    num_rows,
                    col,
                    num_cols,
                ))

                # Input/output index paths.
                partial_index_path_map = self.get_partial_index_path_map(
                    input_data_paths,
                    dir_path,
                    row,
                    col,
                )

                # Handle edge cases.
                if partial_index_path_map is None or \
                   partial_index_path_map["output_index_path"] not in \
                   missing_index_paths:
                    continue

                # Initialize/merge partial indexes.
                if row == 0:
                    timer.push("init-partial")
                    self.encode_partial(partial_index_path_map, dir_path, timer)
                    timer.pop()
                else:
                    timer.push("merge-partial")
                    self.merge_partial(partial_index_path_map, dir_path, timer)
                    timer.pop()

            # Prevent inter-row race condition.
            torch.distributed.barrier()

            timer.pop()

        # Final barrier. [ necessary? ]
        torch.distributed.barrier()

        # Get output index path, for return.
        output_index_path = self.get_added_index_path(input_data_paths, dir_path)

        return output_index_path

    @classmethod
    def time_hnsw(cls, args, timer):
        """Timing model for HNSW cluster assignment."""

        if torch.distributed.get_rank() != 0:
            return

        from lutil import pax
        from tools.retrieval.utils import Timer

        # pax({"max threads": faiss.omp_get_max_threads()})

        timer = Timer()

        timer.push("read-index")
        empty_index_path = "/gpfs/fs1/projects/gpu_adlr/datasets/lmcafee/retrieval/index/faiss-base-corpus-clean/OPQ32_256,IVF4194304_HNSW32,PQ32__t100000000__trained.faissindex"
        index = faiss.read_index(empty_index_path)
        index_ivf = faiss.extract_index_ivf(index)
        quantizer = index_ivf.quantizer
        timer.pop()

        batch_sizes = [ int(a) for a in [ 1e3, 1e6 ] ]
        nprobes = 1, 128, 1024, 4096 # 66000

        # >>>
        # if 1:
        #     data = np.random.rand(10, args.ivf_dim).astype("f4")
        #     D1, I1 = quantizer.search(data, 1)
        #     D2, I2 = quantizer.search(data, 2)
        #     D128, I128 = quantizer.search(data, 128)
        #     # print(np.vstack([ I1[:,0], D1[:,0] ]).T)
        #     # print(np.vstack([ I2[:,0], D2[:,0] ]).T)
        #     # print(np.vstack([ I128[:,0], D128[:,0] ]).T)
        #     print(np.vstack([ I1[:,0], I2[:,0], I128[:,0] ]).T)
        #     print(np.vstack([ D1[:,0], D2[:,0], D128[:,0] ]).T)
        #     # print(I1[:,0])
        #     # print(I2)
        #     # print(I128)
        #     # print(D1)
        #     # print(D2)
        #     # print(D128)
        #     exit(0)
        # <<<

        for batch_size_index, batch_size in enumerate(batch_sizes):

            timer.push("data-%d" % batch_size)
            data = np.random.rand(batch_size, args.ivf_dim).astype("f4")
            timer.pop()

            for nprobe_index, nprobe in enumerate(nprobes):

                timer.push("search-%d-%d" % (batch_size, nprobe))
                D, I = quantizer.search(data, nprobe)
                timer.pop()

                # if nprobe > 1:
                #     D1, I1 = quantizer.search(data, 1)
                #     pax({
                #         "I1" : I1,
                #         "I" : I,
                #     })

                print("time hnsw ... bs %d [ %d/%d ]; nprobe %d [ %d/%d ]." % (
                    batch_size,
                    batch_size_index,
                    len(batch_sizes),
                    nprobe,
                    nprobe_index,
                    len(nprobes),
                ))

        timer.print()
        exit(0)

        pax(0, {
            "index" : index,
            "index_ivf" : index_ivf,
            "quantizer" : quantizer,
            "result" : result,
        })

    @classmethod
    def time_query(cls, args, timer):
        """Timing model for querying."""

        if torch.distributed.get_rank() != 0:
            return

        from lutil import pax
        from tools.retrieval.utils import Timer

        # >>>
        faiss.omp_set_num_threads(1) # 128)
        # pax({"max threads": faiss.omp_get_max_threads()})
        # <<<

        timer = Timer()

        timer.push("read-index")
        added_index_path = "/gpfs/fs1/projects/gpu_adlr/datasets/lmcafee/retrieval/index/faiss-par-add-corpus-clean/OPQ32_256,IVF4194304_HNSW32,PQ32__t100000000/added_064_000-063.faissindex"
        index = faiss.read_index(added_index_path)
        index_ivf = faiss.extract_index_ivf(index)
        timer.pop()

        batch_sizes = [ int(a) for a in [ 1e2, 1e4 ] ]
        # nprobes = 1, 16, 128, 1024, 4096 # 66000
        # nprobes = 2, 4
        nprobes = 4096, # 16, 128
        
        # pax({"index": index})

        for batch_size_index, batch_size in enumerate(batch_sizes):

            timer.push("data-%d" % batch_size)
            opq_data = np.random.rand(batch_size, args.nfeats).astype("f4")
            timer.pop()

            for nprobe_index, nprobe in enumerate(nprobes):

                nnbr = 100

                timer.push("search-%d-%d" % (batch_size, nprobe))

                # >>>
                index_ivf.nprobe = nprobe
                # pax({
                #     "index_ivf" : index_ivf,
                #     "quantizer" : index_ivf.quantizer,
                # })
                # <<<

                timer.push("full")
                index.search(opq_data, nnbr)
                timer.pop()

                timer.push("split")

                timer.push("preproc")
                ivf_data = index.chain.at(0).apply(opq_data)
                timer.pop()

                timer.push("assign")
                D_hnsw, I_hnsw = index_ivf.quantizer.search(ivf_data, nprobe)
                timer.pop()

                # timer.push("pq")
                # I = np.empty((batch_size, nnbr), dtype = "i8")
                # D = np.empty((batch_size, nnbr), dtype = "f4")
                # index_ivf.search_preassigned(
                #     batch_size,
                #     # cls.swig_ptr(I[:,0]),
                #     cls.swig_ptr(ivf_data),
                #     nnbr,
                #     cls.swig_ptr(I_hnsw), # [:,0]),
                #     cls.swig_ptr(D_hnsw), # [:,0]),
                #     cls.swig_ptr(D),
                #     cls.swig_ptr(I),
                #     False,
                # )
                # timer.pop()

                timer.push("swig")
                I = np.empty((batch_size, nnbr), dtype = "i8")
                D = np.empty((batch_size, nnbr), dtype = "f4")
                ivf_data_ptr = cls.swig_ptr(ivf_data)
                I_hnsw_ptr = cls.swig_ptr(I_hnsw)
                D_hnsw_ptr = cls.swig_ptr(D_hnsw)
                D_ptr = cls.swig_ptr(D)
                I_ptr = cls.swig_ptr(I)
                timer.pop()

                timer.push("prefetch")
                index_ivf.invlists.prefetch_lists(I_hnsw_ptr, batch_size * nprobe)
                timer.pop()

                timer.push("search-preassign")
                index_ivf.search_preassigned(
                    batch_size,
                    ivf_data_ptr,
                    nnbr,
                    I_hnsw_ptr,
                    D_hnsw_ptr,
                    D_ptr,
                    I_ptr,
                    True, # False,
                )
                timer.pop()

                timer.pop()

                # pax({"I": I, "D": D})

                # print("time query ... bs %d [ %d/%d ]; nprobe %d [ %d/%d ]." % (
                #     batch_size,
                #     batch_size_index,
                #     len(batch_sizes),
                #     nprobe,
                #     nprobe_index,
                #     len(nprobes),
                # ))

                timer.pop()

        timer.print()
        exit(0)

        pax(0, {
            "index" : index,
            "index_ivf" : index_ivf,
            "quantizer" : quantizer,
            "result" : result,
        })

    @classmethod
    def time_merge_partials(cls, args, timer):
        """Timing model for merging partial indexes."""
    
        if torch.distributed.get_rank() != 0:
            return

        from retrieval.utils import Timer
        timer = Timer()

        get_cluster_ids = lambda n : np.random.randint(
            args.ncluster,
            size = (n, 1),
            dtype = "i8",
        )

        # Num batches & rows.
        batch_size = int(1e6)
        num_batches = 8192 # 1024 # 10
        num_rows = cls.get_num_rows(num_batches)

        raise Exception("switch to IVF4194304.")
        empty_index_path = "/mnt/fsx-outputs-chipdesign/lmcafee/retrieval/index/faiss-decomp-rand-100k/OPQ32_256,IVF1048576_HNSW32,PQ32__t3000000/cluster/ivfpq/empty.faissindex"

        data = np.random.rand(batch_size, args.ivf_dim).astype("f4")

        # Iterate rows
        for row in range(10, num_rows):

            timer.push("row-%d" % row)

            num_cols = cls.get_num_cols(num_batches, row)

            print_rank(0, "r %d / %d, c -- / %d." % (
                row,
                num_rows,
                num_cols,
            ))

            input_index_path = os.path.join(
                "/mnt/fsx-outputs-chipdesign/lmcafee/retrieval/index/tmp",
                "index-r%03d.faissindex" % (row - 1),
            )
            output_index_path = os.path.join(
                "/mnt/fsx-outputs-chipdesign/lmcafee/retrieval/index/tmp",
                "index-r%03d.faissindex" % row,
            )

            # Initialize/merge partial indexes.
            if row == 0:
                timer.push("init-partial")

                timer.push("read")
                index = faiss.read_index(empty_index_path)
                # self.c_verbose(index, True) # too much verbosity, with batch 1M
                # self.c_verbose(index.quantizer, True)
                timer.pop()

                timer.push("cluster-ids")
                cluster_ids = get_cluster_ids(len(data))
                timer.pop()

                timer.push("add-core")
                index.add_core(
                    n = len(data),
                    x = self.swig_ptr(data),
                    xids = self.swig_ptr(np.arange(len(data), dtype = "i8")),
                    precomputed_idx = self.swig_ptr(cluster_ids),
                )
                timer.pop()

                timer.pop()

            else:

                # Output index.
                timer.push("read-output")
                output_index = faiss.read_index(input_index_path)
                output_invlists = output_index.invlists
                timer.pop()

                # Merge input indexes.
                for input_iter in range(1): # output initialized w/ first input

                    timer.push("read-input")
                    input_index = faiss.read_index(input_index_path)
                    input_invlists = input_index.invlists
                    timer.pop()

                    # # timer.push("cluster-ids")
                    # cluster_ids = get_cluster_ids(input_index.ntotal)
                    # # timer.pop()

                    print_rank("ivfpq / merge, input %d / 2. [ +%d -> %d ]"%(
                        input_iter,
                        input_index.ntotal,
                        input_index.ntotal + output_index.ntotal,
                    ))

                    timer.push("add-entry")
                    id_start = output_index.ntotal
                    for list_id in range(input_invlists.nlist):
                        input_list_size = input_invlists.list_size(list_id)
                        if input_list_size == 0:
                            continue
                        ids = self.swig_ptr(np.arange(
                            # output_index.ntotal + input_index.ntotal,
                            id_start,
                            id_start + input_list_size,
                            dtype = "i8",
                        ))
                        # output_invlists.add_entries(
                        #     list_id,
                        #     input_list_size,
                        #     # input_invlists.get_ids(list_id),
                        #     ids,
                        #     input_invlists.get_codes(list_id),
                        # )
                        output_invlists.merge_from(
                            input_invlists,
                            output_index.ntotal,
                        )
                        id_start += input_list_size
                    timer.pop()

                    # output_index.ntotal += input_index.ntotal
                    output_index.ntotal = id_start

                index = output_index

            timer.push("write")
            faiss.write_index(index, output_index_path)
            timer.pop()

            timer.pop()

        timer.print()
        exit(0)
