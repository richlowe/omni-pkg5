#!/usr/bin/python2.4
#
# CDDL HEADER START
#
# The contents of this file are subject to the terms of the
# Common Development and Distribution License (the "License").
# You may not use this file except in compliance with the License.
#
# You can obtain a copy of the license at usr/src/OPENSOLARIS.LICENSE
# or http://www.opensolaris.org/os/licensing.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# When distributing Covered Code, include this CDDL HEADER in each
# file and include the License file at usr/src/OPENSOLARIS.LICENSE.
# If applicable, add the following below this CDDL HEADER, with the
# fields enclosed by brackets "[]" replaced with your own identifying
# information: Portions Copyright [yyyy] [name of copyright owner]
#
# CDDL HEADER END
#
# Copyright 2009 Sun Microsystems, Inc.  All rights reserved.
# Use is subject to license terms.


# Indexer is a class designed to index a set of manifests or pkg plans
# and provide a compact representation on disk which is quickly searchable.

import os
import urllib
import shutil
import errno

import pkg.version

import pkg.fmri as fmri
import pkg.manifest as manifest
import pkg.search_storage as ss
import pkg.search_errors as search_errors
from pkg.misc import EmptyI

# Constants for indicating whether pkgplans or fmri-manifest path pairs are
# used as arguments.
IDX_INPUT_TYPE_PKG = 0
IDX_INPUT_TYPE_FMRI = 1

INITIAL_VERSION_NUMBER = 1

FILE_OPEN_TIMEOUT_SECS = 2

MAX_ADDED_NUMBER_PACKAGES = 20

SORT_FILE_PREFIX = "sort."

# Set max sort file as 128M, which is a tell position of that value.
SORT_FILE_MAX_SIZE = 128 * 1024 * 1024

class Indexer(object):
        """ See block comment at top for documentation """
        file_version_string = "VERSION: "

        def __init__(self, index_dir, get_manifest_func, get_manifest_path_func,
            progtrack=None, excludes=EmptyI, log=None):
                self._num_keys = 0
                self._num_manifests = 0
                self._num_entries = 0
                self.get_manifest_func = get_manifest_func
                self.get_manifest_path_func = get_manifest_path_func
                self.excludes = excludes
                self.__log = log
                
                # This structure was used to gather all index files into one
                # location. If a new index structure is needed, the files can
                # be added (or removed) from here. Providing a list or
                # dictionary allows an easy approach to opening or closing all
                # index files.

                self._data_dict = {
                        "fast_add":
                            ss.IndexStoreSet(ss.FAST_ADD),
                        "fast_remove":
                            ss.IndexStoreSet(ss.FAST_REMOVE),
                        "manf":
                            ss.IndexStoreListDict(ss.MANIFEST_LIST),
                        "full_fmri": ss.IndexStoreSet(ss.FULL_FMRI_FILE),
                        "main_dict": ss.IndexStoreMainDict(ss.MAIN_FILE),
                        "token_byte_offset":
                            ss.IndexStoreDictMutable(ss.BYTE_OFFSET_FILE)
                        }

                self._data_fast_add = self._data_dict["fast_add"]
                self._data_fast_remove = self._data_dict["fast_remove"]
                self._data_manf = self._data_dict["manf"]
                self._data_full_fmri = self._data_dict["full_fmri"]
                self._data_main_dict = self._data_dict["main_dict"]
                self._data_token_offset = self._data_dict["token_byte_offset"]
                
                self._index_dir = index_dir
                self._tmp_dir = os.path.join(self._index_dir, "TMP")

                self._indexed_manifests = 0
                self.server_repo = True
                self.empty_index = False
                self.file_version_number = None

                self._progtrack = progtrack

                self._file_timeout_secs = FILE_OPEN_TIMEOUT_SECS

                self._sort_fh = None
                self._sort_file_num = 0
                self._sort_file_bytes = 0

                self.at_fh = {}
                self.st_fh = {}

        @staticmethod
        def _build_version(vers):
                """ Private method for building versions from a string. """
                return pkg.version.Version(urllib.unquote(vers), None)

        def _read_input_indexes(self, directory):
                """ Opens all index files using consistent_open and reads all
                of them into memory except the main dictionary file to avoid
                inefficient memory usage.

                """
                res = ss.consistent_open(self._data_dict.values(), directory,
                    self._file_timeout_secs)
                if self._progtrack is not None:
                        self._progtrack.index_set_goal(
                            "Reading Existing Index", len(self._data_dict))
                if res == None:
                        self.file_version_number = INITIAL_VERSION_NUMBER
                        self.empty_index = True
                        return None
                self.file_version_number = res

                try:
                        try:
                                for d in self._data_dict.values():
                                        if (d == self._data_main_dict or
                                                d == self._data_token_offset):
                                                if self._progtrack is not None:
                                                        self._progtrack.index_add_progress()
                                                continue
                                        d.read_dict_file()
                                        if self._progtrack is not None:
                                                self._progtrack.index_add_progress()
                        except:
                                self._data_dict["main_dict"].close_file_handle()
                                raise
                finally:
                        for d in self._data_dict.values():
                                if d == self._data_main_dict:
                                        continue
                                d.close_file_handle()
                if self._progtrack is not None:
                        self._progtrack.index_done()

        def __close_sort_fh(self):
                self._sort_fh.close()
                tmp_file_name = os.path.join(self._tmp_dir,
                    SORT_FILE_PREFIX + str(self._sort_file_num - 1))
                tmp_fh = file(tmp_file_name, "rb")
                l = tmp_fh.readlines()
                tmp_fh.close()
                l.sort()
                tmp_fh = file(tmp_file_name, "wb")
                tmp_fh.writelines(l)
                tmp_fh.close()

        def _add_terms(self, pfmri, new_dict):
                pfmri = pfmri.get_fmri(anarchy=True, include_scheme=False)
                p_id = self._data_manf.get_id_and_add(pfmri)
                pfmri = p_id
                
                for tok_tup in new_dict.keys():
                        tok, action_type, subtype, fv = tok_tup
                        lst = [(action_type, [(subtype, [(fv, [(pfmri,
                            list(new_dict[tok_tup]))])])])]
                        s = ss.IndexStoreMainDict.transform_main_dict_line(tok,
                            lst)
                        if len(s) + self._sort_fh.tell() >= SORT_FILE_MAX_SIZE:
                                self.__close_sort_fh()
                                self._sort_fh = file(os.path.join(self._tmp_dir,
                                    SORT_FILE_PREFIX +
                                    str(self._sort_file_num)), "wb")
                                self._sort_file_num += 1
                        self._sort_fh.write(s)
                return

        def _fast_update(self, filters_pkgplan_list):
                filters, pkgplan_list = filters_pkgplan_list
                for p in pkgplan_list:
                        d_fmri, o_fmri = p

                        if d_fmri:
                                self._data_full_fmri.add_entity(
                                    d_fmri.get_fmri(anarchy=True))
                                d_tmp = d_fmri.get_fmri(anarchy=True,
                                    include_scheme=False)
                                assert not self._data_fast_add.has_entity(d_tmp)
                                if self._data_fast_remove.has_entity(d_tmp):
                                        self._data_fast_remove.remove_entity(
                                            d_tmp)
                                else:
                                        self._data_fast_add.add_entity(d_tmp)
                        if o_fmri:
                                self._data_full_fmri.remove_entity(
                                    o_fmri.get_fmri(anarchy=True))
                                o_tmp = o_fmri.get_fmri(anarchy=True,
                                    include_scheme=False)
                                assert not self._data_fast_remove.has_entity(
                                    o_tmp)
                                if self._data_fast_add.has_entity(o_tmp):
                                        self._data_fast_add.remove_entity(o_tmp)
                                else:
                                        self._data_fast_remove.add_entity(o_tmp)
                        
                        if self._progtrack is not None:
                                self._progtrack.index_add_progress()
                return

        def _process_fmris(self, fmris):
                """ Takes a list of fmris and updates the
                internal storage to reflect the new packages.
                """
                removed_paths = []

                for added_fmri in fmris:
                        self._data_full_fmri.add_entity(
                            added_fmri.get_fmri(anarchy=True))
                        new_dict = manifest.Manifest.search_dict(
                            self.get_manifest_path_func(added_fmri),
                            self.excludes, log=self.__log)
                        self._add_terms(added_fmri, new_dict)

                        if self._progtrack is not None:
                                self._progtrack.index_add_progress()
                return removed_paths

        def _write_main_dict_line(self, file_handle, token,
            fv_fmri_pos_list_list, out_dir):
                """ Writes out the new main dictionary file and also adds the
                token offsets to _data_token_offset.
                """

                cur_location = str(file_handle.tell())
                self._data_token_offset.write_entity(token, cur_location)

                for at, st_list in fv_fmri_pos_list_list:
                        if at not in self.at_fh:
                                self.at_fh[at] = file(os.path.join(out_dir,
                                    "__at_" + at), "wb")
                        self.at_fh[at].write(cur_location)
                        self.at_fh[at].write("\n")
                        for st, fv_list in st_list:
                                if st not in self.st_fh:
                                        self.st_fh[st] = \
                                            file(os.path.join(out_dir,
                                            "__st_" + st), "wb")
                                self.st_fh[st].write(cur_location)
                                self.st_fh[st].write("\n")
                                for fv, p_list in fv_list:
                                        for p_id, m_off_set in p_list:
                                                p_id = int(p_id)
                                                pfmri = self._data_manf.get_entity(p_id)
                                                pfmri = fmri.PkgFmri(pfmri)
                                                dir = os.path.join(out_dir,
                                                    "pkg",
                                                    pfmri.get_pkg_stem(
                                                    anarchy=True,
                                                    include_scheme=False))
                                                if not os.path.exists(dir):
                                                        os.makedirs(dir)
                                                path = os.path.join(dir,
                                                    str(pfmri.version))
                                                fh = open(path, "ab")
                                                fh.write(cur_location)
                                                fh.write("\n")
                                                fh.close()

                
                self._data_main_dict.write_main_dict_line(file_handle,
                    token, fv_fmri_pos_list_list)


        @staticmethod
        def __splice(ret_list, source_list):
                tmp_res = []
                for val, sublist in source_list:
                        found = False
                        for r_val, r_sublist in ret_list:
                                if val == r_val:
                                        found = True
                                        Indexer.__splice(r_sublist, sublist)
                                        break
                        if not found:
                                tmp_res.append((val, sublist))
                ret_list.extend(tmp_res)

        def _gen_new_toks_from_files(self):
                def get_line(fh):
                        try:
                                return ss.IndexStoreMainDict.parse_main_dict_line(fh.next())
                        except StopIteration:
                                return None
                fh_dict = dict([
                    (i, file(os.path.join(self._tmp_dir,
                    SORT_FILE_PREFIX + str(i))))
                    for i in range(self._sort_file_num)
                ])
                cur_toks = {}
                for i in fh_dict.keys():
                        line = get_line(fh_dict[i])
                        if line is None:
                                del fh_dict[i]
                        else:
                                cur_toks[i] = line
                while cur_toks:
                        min_token = None
                        matches = []
                        for i in fh_dict.keys():
                                cur_tok, info = cur_toks[i]
                                if cur_tok is None:
                                        continue
                                if min_token is None or cur_tok < min_token:
                                        min_token = cur_tok
                                        matches = [i]
                                elif cur_tok == min_token:
                                        matches.append(i)
                        assert min_token is not None
                        assert len(matches) > 0
                        res = None
                        for i in matches:
                                new_tok, new_info = cur_toks[i]
                                assert new_tok == min_token
                                try:
                                        while new_tok == min_token:
                                                if res is None:
                                                        res = new_info
                                                else:
                                                        self.__splice(res, new_info)
                                                new_tok, new_info = \
                                                    ss.IndexStoreMainDict.parse_main_dict_line(fh_dict[i].next())
                                        cur_toks[i] = new_tok, new_info
                                except StopIteration:
                                        fh_dict[i].close()
                                        del fh_dict[i]
                                        del cur_toks[i]
                        assert res is not None
                        yield min_token, res
                return
                
        def _update_index(self, dicts, out_dir):
                """ Processes the main dictionary file and writes out a new
                main dictionary file reflecting the changes in the packages.
                """
                removed_paths = dicts

                if self.empty_index:
                        file_handle = []
                else:
                        file_handle = self._data_main_dict.get_file_handle()
                        assert file_handle

                if self.file_version_number == None:
                        self.file_version_number = INITIAL_VERSION_NUMBER
                else:
                        self.file_version_number += 1

                self._data_main_dict.write_dict_file(
                    out_dir, self.file_version_number)
                # The dictionary file's opened in append mode to avoid removing
                # the version information the search storage class added.
                out_main_dict_handle = \
                    open(os.path.join(out_dir,
                        self._data_main_dict.get_file_name()), "ab",
                        buffering=131072)

                self._data_token_offset.open_out_file(out_dir,
                    self.file_version_number)

                new_toks_available = True
                new_toks_it = self._gen_new_toks_from_files()
                try:
                        tmp = new_toks_it.next()
                        next_new_tok, new_tok_info = tmp
                except StopIteration:
                        new_toks_available = False

                try:
                        for line in file_handle:
                                (tok, at_lst) = \
                                    self._data_main_dict.parse_main_dict_line(
                                    line)
                                existing_entries = []
                                for at, st_list in at_lst:
                                        st_res = []
                                        for st, fv_list in st_list:
                                                fv_res = []
                                                for fv, p_list in fv_list:
                                                        p_res = []
                                                        for p_id, m_off_set in \
                                                                    p_list:
                                                                p_id = int(p_id)
                                                                pfmri = self._data_manf.get_entity(p_id)
                                                                if pfmri not in removed_paths:
                                                                        p_res.append((p_id, m_off_set))
                                                        if p_res:
                                                                fv_res.append(
                                                                    (fv, p_res))
                                                if fv_res:
                                                        st_res.append(
                                                            (st, fv_res))
                                        if st_res:
                                                existing_entries.append(
                                                    (at, st_res))
                                # Add tokens newly discovered in the added
                                # packages which are alphabetically earlier
                                # than the token most recently read from the
                                # existing main dictionary file.
                                while new_toks_available and next_new_tok < tok:
                                        assert len(next_new_tok) > 0
                                        self._write_main_dict_line(
                                            out_main_dict_handle, next_new_tok,
                                            new_tok_info, out_dir)
                                        try:
                                                next_new_tok, new_tok_info = \
                                                    new_toks_it.next()
                                        except StopIteration:
                                                new_toks_available = False
                                                del next_new_tok
                                                del new_tok_info

                                # Combine the information about the current
                                # token from the new packages with the existing
                                # information for that token.
                                if new_toks_available and next_new_tok == tok:
                                        self.__splice(existing_entries,
                                            new_tok_info)
                                        try:
                                                next_new_tok, new_tok_info = \
                                                    new_toks_it.next()
                                        except StopIteration:
                                                new_toks_available = False
                                                del next_new_tok
                                                del new_tok_info
                                # If this token has any packages still
                                # associated with it, write them to the file.
                                if existing_entries:
                                        assert len(tok) > 0
                                        self._write_main_dict_line(
                                            out_main_dict_handle,
                                            tok, existing_entries, out_dir)
                finally:
                        if not self.empty_index:
                                file_handle.close()
                                self._data_main_dict.close_file_handle()

                # For any new tokens which are alphabetically after the last
                # entry in the existing file, add them to the end of the file.
                while new_toks_available:
                        assert len(next_new_tok) > 0
                        self._write_main_dict_line(
                            out_main_dict_handle, next_new_tok,
                            new_tok_info, out_dir)
                        try:
                                next_new_tok, new_tok_info = new_toks_it.next()
                        except StopIteration:
                                new_toks_available = False
                out_main_dict_handle.close()
                self._data_token_offset.close_file_handle()

                removed_paths = []

        def _write_assistant_dicts(self, out_dir):
                """ Write out the companion dictionaries needed for
                translating the internal representation of the main
                dictionary into human readable information. """
                for d in self._data_dict.values():
                        if d == self._data_main_dict or \
                            d == self._data_token_offset:
                                continue
                        d.write_dict_file(out_dir, self.file_version_number)
                        
        def _generic_update_index(self, inputs, input_type,
            tmp_index_dir=None, image=None):
                """ Performs all the steps needed to update the indexes."""
                
                # Allow the use of a directory other than the default
                # directory to store the intermediate results in.
                if not tmp_index_dir:
                        tmp_index_dir = self._tmp_dir
                assert not (tmp_index_dir == self._index_dir)

                # Read the existing dictionaries.
                self._read_input_indexes(self._index_dir)

                
                try:
                        # If the tmp_index_dir exists, it suggests a previous
                        # indexing attempt aborted or that another indexer is
                        # running. In either case, throw an exception.
                        try:
                                os.makedirs(os.path.join(tmp_index_dir))
                                os.makedirs(os.path.join(tmp_index_dir, "pkg"))
                        except OSError, e:
                                if e.errno == errno.EEXIST:
                                        raise search_errors.PartialIndexingException(tmp_index_dir)
                                else:
                                        raise
                        inputs = list(inputs)
                        skip = False

                        if input_type == IDX_INPUT_TYPE_PKG:
                                assert image
                                if self._progtrack is not None:
                                        self._progtrack.index_set_goal(
                                            "Indexing Packages",
                                            len(inputs[1]))
                                self._fast_update(inputs)
                                skip = True
                                if len(self._data_fast_add._set) > \
                                    MAX_ADDED_NUMBER_PACKAGES:
                                        self._data_main_dict.close_file_handle()
                                        if self._progtrack:
                                                self._progtrack.index_optimize()
                                        image.rebuild_search_index(
                                            self._progtrack)
                                        return

                        elif input_type == IDX_INPUT_TYPE_FMRI:
                                assert not self._sort_fh
                                self._sort_fh = file(os.path.join(self._tmp_dir,
                                    SORT_FILE_PREFIX +
                                    str(self._sort_file_num)), "wb")
                                self._sort_file_num += 1

                                if self._progtrack is not None:
                                        self._progtrack.index_set_goal(
                                            "Indexing Packages",
                                            len(inputs))
                                dicts = self._process_fmris(inputs)
                                # Update the main dictionary file
                                self.__close_sort_fh()
                                self._update_index(dicts, tmp_index_dir)

                                self.empty_index = False
                        else:
                                raise RuntimeError(
                                    "Got unknown input_type: %s", input_type)

                        # Write out the helper dictionaries
                        self._write_assistant_dicts(tmp_index_dir)

                        # Move all files from the tmp directory into the index
                        # dir. Note: the need for consistent_open is that
                        # migrate is not an atomic action.
                        self._migrate(source_dir = tmp_index_dir, skip=skip)

                        if self._progtrack is not None:
                                self._progtrack.index_done()
                finally:
                        self._data_main_dict.close_file_handle()
                
        def client_update_index(self, pkgplan_list, image, tmp_index_dir = None):
                """ This version of update index is designed to work with the
                client side of things. Specifically, it expects a pkg plan
                list with added and removed FMRIs/manifests. Note: if
                tmp_index_dir is specified, it must NOT exist in the current
                directory structure. This prevents the indexer from
                accidentally removing files.
                """
                assert self._progtrack is not None

                self._generic_update_index(pkgplan_list, IDX_INPUT_TYPE_PKG,
                    tmp_index_dir=tmp_index_dir, image=image)

        def server_update_index(self, fmris, tmp_index_dir = None):
                """ This version of update index is designed to work with the
                server side of things. Specifically, since we don't currently
                support removal of a package from a repo, this function simply
                takes a list of FMRIs to be added to the repot. Currently, the
                only way to remove a package from the index is to remove it
                from the depot and reindex. Note: if tmp_index_dir is
                specified, it must NOT exist in the current directory structure.
                This prevents the indexer from accidentally removing files.
                """
                self._generic_update_index(fmris,
                    IDX_INPUT_TYPE_FMRI, tmp_index_dir)

        def check_index_existence(self):
                """ Returns a boolean value indicating whether a consistent
                index exists.

                """
                try:
                        try:
                                res = \
                                    ss.consistent_open(self._data_dict.values(),
                                        self._index_dir,
                                        self._file_timeout_secs)
                        except KeyboardInterrupt:
                                raise
                        except Exception:
                                return False
                finally:
                        for d in self._data_dict.values():
                                d.close_file_handle()
                assert res is not 0
                return res

        def rebuild_index_from_scratch(self, fmris,
            tmp_index_dir = None):
                """ Removes any existing index directory and rebuilds the
                index based on the fmris and manifests provided as an
                argument.
                """
                self.file_version_number = INITIAL_VERSION_NUMBER
                self.empty_index = True
                
                try:
                        shutil.rmtree(self._index_dir)
                        os.makedirs(self._index_dir)
                except OSError, e:
                        if e.errno == errno.EACCES:
                                raise search_errors.ProblematicPermissionsIndexException(
                                    self._index_dir)
                self._generic_update_index(fmris,
                    IDX_INPUT_TYPE_FMRI, tmp_index_dir)
                self.empty_index = False

        def setup(self):
                """ Seeds the index directory with empty stubs if the directory
                is consistently empty. Does not overwrite existing indexes.
                """
                absent = False
                present = False

                if not os.path.exists(os.path.join(self._index_dir, "pkg")):
                        os.makedirs(os.path.join(self._index_dir, "pkg"))
                
                for d in self._data_dict.values():
                        file_path = os.path.join(self._index_dir,
                            d.get_file_name())
                        if os.path.exists(file_path):
                                present = True
                        else:
                                absent = True
                        if absent and present:
                                raise \
                                    search_errors.InconsistentIndexException( \
                                        self._index_dir)
                if present:
                        return
                if self.file_version_number:
                        raise RuntimeError("Got file_version_number other"
                                           "than None in setup.")
                self.file_version_number = INITIAL_VERSION_NUMBER
                for d in self._data_dict.values():
                        d.write_dict_file(self._index_dir,
                            self.file_version_number)

        @staticmethod
        def check_for_updates(index_root, fmri_set):
                """ Checks fmri_set to see which members have not been indexed.
                It modifies fmri_set.
                """
                data =  ss.IndexStoreSet("full_fmri_list")
                try:
                        data.open(index_root)
                except IOError, e:
                        if not os.path.exists(os.path.join(
                                index_root, data.get_file_name())):
                                return fmri_set
                        else:
                                raise
                try:
                        data.read_and_discard_matching_from_argument(fmri_set)
                finally:
                        data.close_file_handle()

        def _migrate(self, source_dir=None, dest_dir=None, skip=False):
                """ Moves the indexes from a temporary directory to the
                permanent one.
                """
                if not source_dir:
                        source_dir = self._tmp_dir
                if not dest_dir:
                        dest_dir = self._index_dir
                assert not (source_dir == dest_dir)
                try:
                        shutil.rmtree(os.path.join(dest_dir, "pkg"))
                except EnvironmentError, e:
                        if e.errno not in (errno.ENOENT, errno.ESRCH):
                                raise
                
                shutil.move(os.path.join(source_dir, "pkg"),
                    os.path.join(dest_dir, "pkg"))
                
                for d in self._data_dict.values():
                        if skip and (d == self._data_main_dict or
                            d == self._data_token_offset):
                                continue
                        else:
                                shutil.move(os.path.join(source_dir,
                                    d.get_file_name()),
                                    os.path.join(dest_dir, d.get_file_name()))
                for at, fh in self.at_fh.items():
                        fh.close()
                        shutil.move(os.path.join(source_dir, "__at_" + at),
                                    os.path.join(dest_dir, "__at_" + at))

                for st, fh in self.st_fh.items():
                        fh.close()
                        shutil.move(os.path.join(source_dir, "__st_" + st),
                                    os.path.join(dest_dir, "__st_" + st))
                shutil.rmtree(source_dir)
