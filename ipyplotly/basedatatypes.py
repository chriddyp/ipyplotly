import collections
import re
import typing as typ
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pprint import pprint
import numbers
from importlib import import_module

import ipywidgets as widgets

from ipyplotly import animation
from ipyplotly.basevalidators import CompoundValidator, CompoundArrayValidator
from ipyplotly.serializers import custom_serializers
from traitlets import List, Unicode, Dict, observe, Integer, Undefined

from ipyplotly.callbacks import Points, BoxSelector, LassoSelector, InputState
from ipyplotly.validators.layout import XaxisValidator, YaxisValidator, GeoValidator, TernaryValidator, SceneValidator

from plotly.offline import plot as plotlypy_plot
import os
import numpy as np
from urllib import parse

@widgets.register
class BaseFigureWidget(widgets.DOMWidget):

    # Traits
    # ------
    _view_name = Unicode('FigureView').tag(sync=True)
    _view_module = Unicode('ipyplotly').tag(sync=True)
    _model_name = Unicode('FigureModel').tag(sync=True)
    _model_module = Unicode('ipyplotly').tag(sync=True)

    # Data properties for front end
    # Note: These are only automatically synced on full assignment, not on mutation
    _layout = Dict().tag(sync=True, **custom_serializers)
    _data = List().tag(sync=True, **custom_serializers)

    # Python -> JS message properties
    _py2js_addTraces = List(trait=Dict(),
                            allow_none=True).tag(sync=True, **custom_serializers)

    _py2js_restyle = List(allow_none=True).tag(sync=True, **custom_serializers)
    _py2js_relayout = Dict(allow_none=True).tag(sync=True, **custom_serializers)
    _py2js_update = List(allow_none=True).tag(sync=True, **custom_serializers)
    _py2js_animate = List(allow_none=True).tag(sync=True, **custom_serializers)

    _py2js_deleteTraces = Dict(allow_none=True).tag(sync=True, **custom_serializers)
    _py2js_moveTraces = List(allow_none=True).tag(sync=True, **custom_serializers)

    _py2js_removeLayoutProps = List(allow_none=True).tag(sync=True, **custom_serializers)
    _py2js_removeStyleProps = List(allow_none=True).tag(sync=True, **custom_serializers)
    _py2js_requestSvg = Unicode(allow_none=True).tag(sync=True)

    # JS -> Python message properties
    _js2py_styleDelta = List(allow_none=True).tag(sync=True, **custom_serializers)
    _js2py_layoutDelta = Dict(allow_none=True).tag(sync=True, **custom_serializers)
    _js2py_restyle = List(allow_none=True).tag(sync=True, **custom_serializers)
    _js2py_relayout = Dict(allow_none=True).tag(sync=True, **custom_serializers)
    _js2py_update = Dict(allow_none=True).tag(sync=True, **custom_serializers)

    # For plotly_select/hover/unhover/click
    _js2py_pointsCallback = Dict(allow_none=True).tag(sync=True, **custom_serializers)

    # Message tracking
    _last_relayout_msg_id = Integer(0).tag(sync=True)
    _last_restyle_msg_id = Integer(0).tag(sync=True)

    # Constructor
    # -----------
    def __init__(self, data=None, layout=None):
        super().__init__()

        # Traces
        # ------
        from ipyplotly.validators import TracesValidator
        self._data_validator = TracesValidator()

        if data is None:
            self._data_objs = ()  # type: typ.Tuple[BaseTraceHierarchyType]
            self._data_defaults = []
        else:
            data = self._data_validator.validate_coerce(data)

            self._data_objs = data
            self._data_defaults = [{} for trace in data]
            self._data = [deepcopy(trace._props) for trace in data]
            for trace in data:
                trace._orphan_props.clear()
                trace._parent = self

        # Layout
        # ------
        from ipyplotly.validators import LayoutValidator
        self._layout_validator = LayoutValidator()

        from ipyplotly.datatypes import Layout

        if layout is None:
            layout = Layout()  # type: Layout
        else:
            layout = self._layout_validator.validate_coerce(layout)

        self._layout_obj = layout
        self._layout = deepcopy(self._layout_obj._props)
        self._layout_obj._parent = self
        self._layout_defaults = {}

        # Message States
        # --------------
        self._relayout_in_process = False
        self._waiting_relayout_callbacks = []

        self._restyle_in_process = False
        self._waiting_restyle_callbacks = []

        # View count
        # ----------
        self._view_count = 0

        # Context manager
        # ---------------
        self._in_batch_mode = False
        self._batch_style_commands = {}  # type: typ.Dict[int, typ.Dict[str, typ.Any]]
        self._batch_layout_commands = {}  # type: typ.Dict[str, typ.Any]
        self._animation_duration_validator = animation.DurationValidator()
        self._animation_easing_validator = animation.EasingValidator()

        # SVG
        # ---
        self._svg_requests = {}

        # Messages
        # --------
        self.on_msg(self._handler_messages)

        # Logging
        # -------
        self._log_plotly_commands = False

    # ### Trait methods ###
    @observe('_js2py_styleDelta')
    def handler_plotly_styleDelta(self, change):
        deltas = change['new']
        self._js2py_styleDelta = None

        if not deltas:
            return

        msg_id = deltas[0].get('_restyle_msg_id', None)
        # print(f'styleDelta: {msg_id} == {self._last_restyle_msg_id}')
        if msg_id == self._last_restyle_msg_id:
            for delta in deltas:
                trace_uid = delta['uid']

                # Remove message id
                # pprint(delta)
                # print('Processing styleDelta')

                trace_uids = [trace.uid for trace in self.data]
                trace_index = trace_uids.index(trace_uid)
                uid_trace = self.data[trace_index]
                delta_transform = BaseFigureWidget.transform_data(uid_trace._prop_defaults, delta)

                removed_props = self.remove_overlapping_props(uid_trace._props, uid_trace._prop_defaults)

                if removed_props:
                    # print(f'Removed_props: {removed_props}')
                    self._py2js_removeStyleProps = [removed_props, trace_index]
                    self._py2js_removeStyleProps = None

                # print(delta_transform)
                self._dispatch_change_callbacks_restyle(delta_transform, [trace_index])

            self._restyle_in_process = False
            while self._waiting_restyle_callbacks:
                # Call callbacks
                self._waiting_restyle_callbacks.pop()()

    @observe('_js2py_restyle')
    def handler_js2py_restyle(self, change):
        restyle_msg = change['new']
        self._js2py_restyle = None

        if not restyle_msg:
            return

        self.restyle(*restyle_msg)

    @observe('_js2py_update')
    def handler_js2py_update(self, change):
        update_msg = change['new']
        self._js2py_update = None

        if not update_msg:
            return

        # print('Update (JS->Py):')
        # pprint(update_msg)

        style = update_msg['data'][0]
        trace_indexes = update_msg['data'][1]
        layout = update_msg['layout']

        self.update(style=style, layout=layout, trace_indexes=trace_indexes)

    # Magic Methods
    # -------------
    def __setitem__(self, prop, value):
        if prop == 'data':
            self.data = value
        elif prop == 'layout':
            self.layout = value
        else:
            raise KeyError(prop)

    def __getitem__(self, prop):
        if prop == 'data':
            return self.data
        elif prop == 'layout':
            return self.layout
        else:
            raise KeyError(prop)

    # Data
    # ----
    @property
    def data(self) -> typ.Tuple['BaseTraceType']:
        return self._data_objs

    @data.setter
    def data(self, new_data):

        # Validate new_data
        orig_uids = [_trace['uid'] for _trace in self._data]
        new_uids = [trace.uid for trace in new_data]

        invalid_uids = set(new_uids).difference(set(orig_uids))
        if invalid_uids:
            raise ValueError(('The trace property of a figure may only be assigned to '
                              'a permutation of a subset of itself\n'
                              '    Invalid trace(s) with uid(s): {invalid_uids}').format(invalid_uids=invalid_uids))

        # Check for duplicates
        uid_counter = collections.Counter(new_uids)
        duplicate_uids = [uid for uid, count in uid_counter.items() if count > 1]
        if duplicate_uids:
            raise ValueError(('The trace property of a figure may not be assigned '
                              'multiple copies of a trace\n'
                              '    Duplicate trace uid(s): {duplicate_uids}'
                              ).format(duplicate_uids=duplicate_uids))

        # Compute traces to remove
        remove_uids = set(orig_uids).difference(set(new_uids))
        delete_inds = []
        for i, _trace in enumerate(self._data):
            if _trace['uid'] in remove_uids:
                delete_inds.append(i)

                # Unparent trace object to be removed
                old_trace = self.data[i]
                old_trace._orphan_props.update(deepcopy(self.data[i]._props))
                old_trace._parent = None

        # Compute trace data list after removal
        traces_props_post_removal = [t for t in self._data]
        traces_prop_defaults_post_removal = [t for t in self._data_defaults]
        orig_uids_post_removal = [trace_data['uid'] for trace_data in self._data]

        for i in reversed(delete_inds):
            del traces_props_post_removal[i]
            del traces_prop_defaults_post_removal[i]
            del orig_uids_post_removal[i]

        if delete_inds:
            relayout_msg_id = self._last_relayout_msg_id + 1
            self._last_relayout_msg_id = relayout_msg_id
            self._relayout_in_process = True

            for di in reversed(delete_inds):
                del self._data[di]  # Modify in-place so we don't trigger serialization

            if self._log_plotly_commands:
                print('Plotly.deleteTraces')
                pprint(delete_inds, indent=4)

            self._py2js_deleteTraces = {'delete_inds': delete_inds,
                                        '_relayout_msg_id ': relayout_msg_id}
            self._py2js_deleteTraces = None

        # Compute move traces
        new_inds = []

        for uid in orig_uids_post_removal:
            new_inds.append(new_uids.index(uid))

        current_inds = list(range(len(traces_props_post_removal)))

        if not all([i1 == i2 for i1, i2 in zip(new_inds, current_inds)]):

            move_msg = [current_inds, new_inds]

            if self._log_plotly_commands:
                print('Plotly.moveTraces')
                pprint(move_msg, indent=4)

            self._py2js_moveTraces = move_msg
            self._py2js_moveTraces = None

            # ### Reorder trace elements ###
            # We do so in-place so we don't trigger serialization
            # pprint(self._traces_data)

            # #### Remove by curr_inds in reverse order ####
            moving_traces_data = []
            for ci in reversed(current_inds):
                # Push moving traces data to front of list
                moving_traces_data.insert(0, self._data[ci])
                del self._data[ci]

            # #### Sort new_inds and moving_traces_data by new_inds ####
            new_inds, moving_traces_data = zip(*sorted(zip(new_inds, moving_traces_data)))

            # #### Insert by new_inds in forward order ####
            for ni, trace_data in zip(new_inds, moving_traces_data):
                self._data.insert(ni, trace_data)

            # pprint(self._traces_data)

        # Update _traces order
        self._data_defaults = [_trace for i, _trace in sorted(zip(new_inds, traces_prop_defaults_post_removal))]
        self._data_objs = tuple(new_data)

    def restyle(self, style, trace_indexes=None):
        if trace_indexes is None:
            trace_indexes = list(range(len(self.data)))

        if not isinstance(trace_indexes, (list, tuple)):
            trace_indexes = [trace_indexes]

        restyle_msg = self._perform_restyle_dict(style, trace_indexes)
        if restyle_msg:
            self._dispatch_change_callbacks_restyle(restyle_msg, trace_indexes)
            self._send_restyle_msg(restyle_msg, trace_indexes=trace_indexes)

    def _perform_restyle_dict(self, style, trace_indexes):
        # Make sure trace_indexes is an array
        if not isinstance(trace_indexes, list):
            trace_indexes = [trace_indexes]

        restyle_data = {}  # Resytyle data to send to JS side as Plotly.restylePlot()

        for raw_key, v in style.items():
            # kstr may have periods. e.g. foo.bar
            key_path = self._str_to_dict_path(raw_key)

            # Properties with leading underscores passed through as-is
            if raw_key.startswith('_'):
                restyle_data[raw_key] = v
                continue

            if not isinstance(v, list):
                v = [v]

            if isinstance(v, dict):
                raise ValueError('Restyling objects not supported, only individual properties\n'
                                 '    Received: {{k}: {v}}'.format(k=raw_key, v=v))
            else:
                restyle_msg_vs = []
                any_vals_changed = False
                for i, trace_ind in enumerate(trace_indexes):
                    if trace_ind >= len(self._data):
                        raise ValueError('Trace index {trace_ind} out of range'.format(trace_ind=trace_ind))
                    val_parent = self._data[trace_ind]
                    for kp, key_path_el in enumerate(key_path[:-1]):

                        # Extend val_parent list if needed
                        if isinstance(val_parent, list) and isinstance(key_path_el, int):
                            while len(val_parent) <= key_path_el:
                                val_parent.append(None)

                        elif isinstance(val_parent, dict) and key_path_el not in val_parent:
                            if isinstance(key_path[kp + 1], int):
                                val_parent[key_path_el] = []
                            else:
                                val_parent[key_path_el] = {}

                        val_parent = val_parent[key_path_el]

                    last_key = key_path[-1]

                    trace_v = v[i % len(v)]

                    restyle_msg_vs.append(trace_v)

                    if BasePlotlyType._vals_equal(trace_v, Undefined):
                        # Do nothing
                        pass
                    elif trace_v is None:
                        if isinstance(val_parent, dict) and last_key in val_parent:
                            val_parent.pop(last_key)
                            any_vals_changed = True
                    elif isinstance(val_parent, dict):
                        if last_key not in val_parent or not BasePlotlyType._vals_equal(val_parent[last_key], trace_v):
                            val_parent[last_key] = trace_v
                            any_vals_changed = True

                if any_vals_changed:
                    # At lease one of the values for one of the traces has changed. Update them all
                    restyle_data[raw_key] = restyle_msg_vs

        return restyle_data

    def _dispatch_change_callbacks_restyle(self, style, trace_indexes):
        if not isinstance(trace_indexes, list):
            trace_indexes = [trace_indexes]

        dispatch_plan = {t: {} for t in trace_indexes}
        # e.g. {0: {(): {'obj': layout,
        #            'changed_paths': [('xaxis', 'range')]}}}

        for raw_key, v in style.items():
            key_path = self._str_to_dict_path(raw_key)

            # Test whether we should remove trailing integer in path
            # e.g. ('xaxis', 'range', '1') -> ('xaxis', 'range')
            # We only do this if the trailing index is an integer that references a primitive value
            if isinstance(key_path[-1], int) and not isinstance(v, dict):
                key_path = key_path[:-1]

            for trace_ind in trace_indexes:

                parent_obj = self.data[trace_ind]
                key_path_so_far = ()
                keys_left = key_path

                # Iterate down the key path
                for next_key in key_path:
                    if next_key not in parent_obj:
                        # Not a property
                        break

                    if isinstance(parent_obj, BasePlotlyType):
                        if key_path_so_far not in dispatch_plan[trace_ind]:
                            dispatch_plan[trace_ind][key_path_so_far] = {'obj': parent_obj, 'changed_paths': set()}

                        dispatch_plan[trace_ind][key_path_so_far]['changed_paths'].add(keys_left)

                        next_val = parent_obj[next_key]
                    elif isinstance(parent_obj, (list, tuple)):
                        next_val = parent_obj[next_key]
                    else:
                        # Primitive value
                        break

                    key_path_so_far = key_path_so_far + (next_key,)
                    keys_left = keys_left[1:]
                    parent_obj = next_val

        # pprint(dispatch_plan)
        for trace_ind in trace_indexes:
            for p in dispatch_plan[trace_ind].values():
                obj = p['obj']
                changed_paths = p['changed_paths']
                obj._dispatch_change_callbacks(changed_paths)

    def _send_restyle_msg(self, style, trace_indexes=None):
        if not isinstance(trace_indexes, (list, tuple)):
            trace_indexes = [trace_indexes]

        # Add and update message ids
        relayout_msg_id = self._last_relayout_msg_id + 1
        style['_relayout_msg_id'] = relayout_msg_id
        self._last_relayout_msg_id = relayout_msg_id
        self._relayout_in_process = True

        restyle_msg_id = self._last_restyle_msg_id + 1
        style['_restyle_msg_id'] = restyle_msg_id
        self._last_restyle_msg_id = restyle_msg_id
        self._restyle_in_process = True

        restyle_msg = (style, trace_indexes)
        if self._log_plotly_commands:
            print('Plotly.restyle')
            pprint(restyle_msg, indent=4)

        self._py2js_restyle = restyle_msg
        self._py2js_restyle = None

    def _restyle_child(self, child, prop, val):

        trace_index = self.data.index(child)

        if not self._in_batch_mode:
            send_val = [val]
            restyle = {prop: send_val}
            self._dispatch_change_callbacks_restyle(restyle, trace_index)
            self._send_restyle_msg(restyle, trace_indexes=trace_index)
        else:
            if trace_index not in self._batch_style_commands:
                self._batch_style_commands[trace_index] = {}
            self._batch_style_commands[trace_index][prop] = val

    def add_traces(self, data: typ.List['BaseTraceType']):

        if self._in_batch_mode:
            self._batch_layout_commands.clear()
            self._batch_style_commands.clear()
            raise ValueError('Traces may not be added in a batch context')

        if not isinstance(data, (list, tuple)):
            data = [data]

        # Validate
        data = self._data_validator.validate_coerce(data)

        # Make deep copy of trace data (Optimize later if needed)
        new_traces_data = [deepcopy(trace._props) for trace in data]

        # Update trace parent
        for trace in data:
            trace._parent = self
            trace._orphan_props.clear()

        # Update python side
        self._data.extend(new_traces_data)  # append instead of assignment so we don't trigger serialization
        self._data_defaults = self._data_defaults + [{} for trace in data]
        self._data_objs = self._data_objs + data

        # Update messages
        relayout_msg_id = self._last_relayout_msg_id + 1
        self._last_relayout_msg_id = relayout_msg_id
        self._relayout_in_process = True

        restyle_msg_id = self._last_restyle_msg_id + 1
        self._last_restyle_msg_id = restyle_msg_id
        self._restyle_in_process = True

        for traces_data in new_traces_data:
            traces_data['_relayout_msg_id'] = relayout_msg_id
            traces_data['_restyle_msg_id'] = restyle_msg_id

        # Send to front end
        if self._log_plotly_commands:
            print('Plotly.addTraces')
            pprint(new_traces_data, indent=4)

        add_traces_msg = new_traces_data
        self._py2js_addTraces = add_traces_msg
        self._py2js_addTraces = None

        return data

    def _get_child_props(self, child):
        try:
            trace_index = self.data.index(child)
        except ValueError as _:
            trace_index = None

        if trace_index is not None:
            return self._data[trace_index]
        elif child is self.layout:
            return self._layout
        else:
            raise ValueError('Unrecognized child: %s' % child)

    def _get_child_prop_defaults(self, child):
        try:
            trace_index = self.data.index(child)
        except ValueError as _:
            trace_index = None

        if trace_index is not None:
            return self._data_defaults[trace_index]
        elif child is self.layout:
            return self._layout_defaults
        else:
            raise ValueError('Unrecognized child: %s' % child)

    def _init_child_props(self, child):
        # layout and traces dict are never None
        return

    # Layout
    # ------
    @observe('_js2py_layoutDelta')
    def handler_plotly_layoutDelta(self, change):
        delta = change['new']
        self._js2py_layoutDelta = None

        if not delta:
            return

        msg_id = delta.get('_relayout_msg_id')
        # print(f'layoutDelta: {msg_id} == {self._last_relayout_msg_id}')
        if msg_id == self._last_relayout_msg_id:
            # print('Processing layoutDelta')
            # print('layoutDelta: {deltas}'.format(deltas=delta))
            delta_transform = self.transform_data(self._layout_defaults, delta)
            # print(f'delta_transform: {delta_transform}')

            # No relayout messages in process. Handle removing overlapping properties
            removed_props = self.remove_overlapping_props(self._layout, self._layout_defaults)
            if removed_props:
                # print(f'Removed_props: {removed_props}')
                self._py2js_removeLayoutProps = removed_props
                self._py2js_removeLayoutProps = None

            self._dispatch_change_callbacks_relayout(delta_transform)
            self._relayout_in_process = False
            while self._waiting_relayout_callbacks:
                # Call callbacks
                self._waiting_relayout_callbacks.pop()()

    @property
    def layout(self):
        return self._layout_obj

    @layout.setter
    def layout(self, new_layout):
        # Validate layout
        new_layout = self._layout_validator.validate_coerce(new_layout)
        new_layout_data = deepcopy(new_layout._props)

        # Unparent current layout
        if self._layout_obj:
            old_layout_data = deepcopy(self._layout_obj._props)
            self._layout_obj._orphan_props.update(old_layout_data)
            self._layout_obj._parent = None

        # Parent new layout
        self._layout = new_layout_data
        new_layout._parent = self
        self._layout_obj = new_layout

        # Notify JS side
        self._send_relayout_msg(new_layout_data)

    def _relayout_child(self, child, prop, val):
        send_val = val  # Don't wrap in a list for relayout

        if not self._in_batch_mode:
            relayout_msg = {prop: send_val}
            self._dispatch_change_callbacks_relayout(relayout_msg)
            self._send_relayout_msg(relayout_msg)
        else:
            self._batch_layout_commands[prop] = send_val

    def _send_relayout_msg(self, layout):

        if self._log_plotly_commands:
            print('Plotly.relayout')
            pprint(layout, indent=4)

        msg_id = self._last_relayout_msg_id + 1
        layout['_relayout_msg_id'] = msg_id
        self._last_relayout_msg_id = msg_id

        self._py2js_relayout = layout
        self._py2js_relayout = None

    @observe('_js2py_relayout')
    def handler_js2py_relayout(self, change):
        relayout_data = change['new']
        # print('Relayout (JS->Py):')
        # pprint(relayout_data)

        self._js2py_relayout = None

        if not relayout_data:
            return

        if 'lastInputTime' in relayout_data:
            # Remove 'lastInputTime'. Seems to be an internal plotly property that is introduced for some plot types
            relayout_data.pop('lastInputTime')

        self.relayout(relayout_data)

    def relayout(self, layout):
        relayout_msg = self._perform_relayout_dict(layout)
        if relayout_msg:
            self._dispatch_change_callbacks_relayout(relayout_msg)
            self._send_relayout_msg(relayout_msg)

    def _perform_relayout_dict(self, relayout_data):
        relayout_msg = {}  # relayout data to send to JS side as Plotly.relayout()

        # Update layout_data
        # print('_perform_relayout')
        for raw_key, v in relayout_data.items():
            # kstr may have periods. e.g. foo.bar
            key_path = self._str_to_dict_path(raw_key)

            val_parent = self._layout
            for kp, key_path_el in enumerate(key_path[:-1]):
                if key_path_el not in val_parent:

                    # Extend val_parent list if needed
                    if isinstance(val_parent, list) and isinstance(key_path_el, int):
                        while len(val_parent) <= key_path_el:
                            val_parent.append(None)

                    elif isinstance(val_parent, dict) and key_path_el not in val_parent:
                        if isinstance(key_path[kp+1], int):
                            val_parent[key_path_el] = []
                        else:
                            val_parent[key_path_el] = {}

                val_parent = val_parent[key_path_el]

            last_key = key_path[-1]
            # print(f'{val_parent}, {key_path}, {last_key}, {v}')

            if v is Undefined:
                # Do nothing
                pass
            elif v is None:
                if isinstance(val_parent, dict) and last_key in val_parent:
                    val_parent.pop(last_key)
                    relayout_msg[raw_key] = None
            else:
                if isinstance(val_parent, list):
                    if isinstance(last_key, int):
                        while(len(val_parent) <= last_key):
                            val_parent.append(None)
                        val_parent[last_key] = v
                        relayout_msg[raw_key] = v
                elif isinstance(val_parent, dict):
                    if last_key not in val_parent or not BasePlotlyType._vals_equal(val_parent[last_key], v):
                        val_parent[last_key] = v
                        relayout_msg[raw_key] = v

        return relayout_msg

    def _dispatch_change_callbacks_relayout(self, relayout_msg):
        dispatch_plan = {}  # e.g. {(): {'obj': layout,
                            #            'changed_paths': [('xaxis', 'range')]}}
        for raw_key, v in relayout_msg.items():
            # kstr may have periods. e.g. foo.bar
            key_path = self._str_to_dict_path(raw_key)

            # Test whether we should remove trailing integer in path
            # e.g. ('xaxis', 'range', '1') -> ('xaxis', 'range')
            # We only do this if the trailing index is an integer that references a primitive value
            if isinstance(key_path[-1], int) and not isinstance(v, dict):
                key_path = key_path[:-1]

            parent_obj = self.layout
            key_path_so_far = ()
            keys_left = key_path

            # Iterate down the key path
            for next_key in key_path:
                if next_key not in parent_obj:
                    break

                if isinstance(parent_obj, BasePlotlyType):
                    if key_path_so_far not in dispatch_plan:
                        dispatch_plan[key_path_so_far] = {'obj': parent_obj, 'changed_paths': set()}
                    dispatch_plan[key_path_so_far]['changed_paths'].add(keys_left)

                    next_val = parent_obj[next_key]
                    # parent_obj._dispatch_change_callbacks(next_key, next_val)
                elif isinstance(parent_obj, (list, tuple)):
                    next_val = parent_obj[next_key]
                else:
                    # Primitive value
                    break

                key_path_so_far = key_path_so_far + (next_key,)
                keys_left = keys_left[1:]
                parent_obj = next_val

        # pprint(dispatch_plan)
        for p in dispatch_plan.values():
            obj = p['obj']
            changed_paths = p['changed_paths']
            obj._dispatch_change_callbacks(changed_paths)

    # Update
    # ------
    def update(self, style=None, layout=None, trace_indexes=None):

        restyle_msg, relayout_msg, trace_indexes = self._perform_update_dict(style=style,
                                                                             layout=layout,
                                                                             trace_indexes=trace_indexes)
        # Perform restyle portion of update
        if restyle_msg:
            self._dispatch_change_callbacks_restyle(restyle_msg, trace_indexes)

        # Perform relayout portion of update
        if relayout_msg:
            self._dispatch_change_callbacks_relayout(relayout_msg)

        if restyle_msg or relayout_msg:
            self._send_update_msg(restyle_msg, relayout_msg, trace_indexes)

    def _perform_update_dict(self, style=None, layout=None, trace_indexes=None):
        if not style and not layout:
            # Nothing to do
            return None, None, None

        if style is None:
            style = {}
        if layout is None:
            layout = {}

        # Process trace indexes
        if trace_indexes is None:
            trace_indexes = list(range(len(self.data)))

        if not isinstance(trace_indexes, (list, tuple)):
            trace_indexes = [trace_indexes]

        relayout_msg = self._perform_relayout_dict(layout)
        restyle_msg = self._perform_restyle_dict(style, trace_indexes)
        # print(style, trace_indexes, restyle_msg)
        # pprint(self._traces_data)
        return restyle_msg, relayout_msg, trace_indexes


    def _send_update_msg(self, style, layout, trace_indexes=None):
        if not isinstance(trace_indexes, (list, tuple)):
            trace_indexes = [trace_indexes]

        # Add restyle message id
        restyle_msg_id = self._last_restyle_msg_id + 1
        style['_restyle_msg_id'] = restyle_msg_id
        self._last_restyle_msg_id = restyle_msg_id
        self._restyle_in_process = True

        # Add relayout message id
        relayout_msg_id = self._last_relayout_msg_id + 1
        layout['_relayout_msg_id'] = relayout_msg_id
        self._last_relayout_msg_id = relayout_msg_id
        self._relayout_in_process = True

        update_msg = (style, layout, trace_indexes)

        if self._log_plotly_commands:
            print('Plotly.update')
            pprint(update_msg, indent=4)

        self._py2js_update = update_msg
        self._py2js_update = None

    # Callbacks
    # ---------
    @observe('_js2py_pointsCallback')
    def handler_plotly_pointsCallback(self, change):
        callback_data = change['new']
        self._js2py_pointsCallback = None

        if not callback_data:
            return

        # Get event type
        # --------------
        event_type = callback_data['event_type']

        # Build Selector Object
        # ---------------------
        if callback_data.get('selector', None):
            selector_data = callback_data['selector']
            selector_type = selector_data['type']
            if selector_type == 'box':
                selector = BoxSelector(**selector_data)
            elif selector_type == 'lasso':
                selector = LassoSelector(**selector_data)
            else:
                raise ValueError('Unsupported selector type: %s' % selector_type)
        else:
            selector = None

        # Build State Object
        # ------------------
        if callback_data.get('state', None):
            state_data = callback_data['state']
            state = InputState(**state_data)
        else:
            state = None

        # Build Trace Points Dictionary
        # -----------------------------
        points_data = callback_data['points']
        trace_points = {trace_ind: {'point_inds': [],
                                    'xs': [],
                                    'ys': [],
                                    'trace_name': self._data_objs[trace_ind].name,
                                    'trace_index': trace_ind}
                        for trace_ind in range(len(self._data_objs))}

        for x, y, point_ind, trace_ind in zip(points_data['xs'],
                                                  points_data['ys'],
                                                  points_data['pointNumbers'],
                                                  points_data['curveNumbers']):

            trace_dict = trace_points[trace_ind]
            trace_dict['xs'].append(x)
            trace_dict['ys'].append(y)
            trace_dict['point_inds'].append(point_ind)

        # TODO: send empty points event to all traces that were note included

        # Dispatch callbacks
        # ------------------
        for trace_ind, trace_points_data in trace_points.items():
            points = Points(**trace_points_data)
            trace = self.data[trace_ind]  # type: BaseTraceType

            if event_type == 'plotly_click':
                trace._dispatch_on_click(points, state)
            elif event_type == 'plotly_hover':
                trace._dispatch_on_hover(points, state)
            elif event_type == 'plotly_unhover':
                trace._dispatch_on_unhover(points, state)
            elif event_type == 'plotly_selected':
                trace._dispatch_on_selected(points, selector)

    def on_relayout_completed(self, fn):
        if self._relayout_in_process:
            self._waiting_relayout_callbacks.append(fn)
        else:
            fn()

    def on_restyle_completed(self, fn):
        if self._restyle_in_process:
            self._waiting_restyle_callbacks.append(fn)
        else:
            fn()

    # Context managers
    # ----------------
    @contextmanager
    def batch_update(self):
        """Hold syncing any state until the outermost context manager exits"""
        if self._in_batch_mode is True:
            yield
        else:
            try:
                self._in_batch_mode = True
                yield
            finally:
                self._in_batch_mode = False
                self._send_batch_update()

    def _build_update_params_from_batch(self):
        # Handle Style / Trace Indexes
        # ----------------------------
        batch_style_commands = self._batch_style_commands
        trace_indexes = sorted(set([trace_ind for trace_ind in batch_style_commands]))

        all_props = sorted(set([prop
                                for trace_style in self._batch_style_commands.values()
                                for prop in trace_style]))

        # Initialize style dict with all values undefined
        style = {prop: [Undefined for _ in range(len(trace_indexes))]
                 for prop in all_props}

        # Fill in values
        for trace_ind, trace_style in batch_style_commands.items():
            for trace_prop, trace_val in trace_style.items():
                style[trace_prop][trace_indexes.index(trace_ind)] = trace_val

        # Handle Layout
        # -------------
        layout = self._batch_layout_commands

        return style, layout, trace_indexes

    def _send_batch_update(self):
        style, layout, trace_indexes = self._build_update_params_from_batch()
        self.update(style=style, layout=layout, trace_indexes=trace_indexes)
        self._batch_layout_commands.clear()
        self._batch_style_commands.clear()

    @contextmanager
    def batch_animate(self, duration=500, easing="cubic-in-out"):
        """
        Context manager to animate trace / layout updates

        Parameters
        ----------
        duration : number
            The duration of the transition, in milliseconds. If equal to zero, updates are synchronous.
        easing : string
            The easing function used for the transition.
            One of:
                - linear
                - quad
                - cubic
                - sin
                - exp
                - circle
                - elastic
                - back
                - bounce
                - linear-in
                - quad-in
                - cubic-in
                - sin-in
                - exp-in
                - circle-in
                - elastic-in
                - back-in
                - bounce-in
                - linear-out
                - quad-out
                - cubic-out
                - sin-out
                - exp-out
                - circle-out
                - elastic-out
                - back-out
                - bounce-out
                - linear-in-out
                - quad-in-out
                - cubic-in-out
                - sin-in-out
                - exp-in-out
                - circle-in-out
                - elastic-in-out
                - back-in-out
                - bounce-in-ou

        Returns
        -------
            None
        """
        duration = self._animation_duration_validator.validate_coerce(duration)
        easing = self._animation_easing_validator.validate_coerce(easing)

        if self._in_batch_mode is True:
            yield
        else:
            try:
                self._in_batch_mode = True
                yield
            finally:
                self._in_batch_mode = False
                self._send_batch_animate(
                    {'transition': {'duration': duration,'easing': easing},
                     'frame': {'duration': duration}})

    def _send_batch_animate(self, animation_opts):

        # Apply commands to internal dictionaries as an update
        # ----------------------------------------------------
        style, layout, trace_indexes = self._build_update_params_from_batch()
        restyle_msg, relayout_msg, trace_indexes = self._perform_update_dict(style, layout, trace_indexes)

        # ### Perform restyle portion of animate ###
        if restyle_msg:
            self._dispatch_change_callbacks_restyle(restyle_msg, trace_indexes)

        # ### Perform relayout portion of update ###
        if relayout_msg:
            self._dispatch_change_callbacks_relayout(relayout_msg)

        # Convert style / trace_indexes into animate form
        # -----------------------------------------------
        if self._batch_style_commands:
            animate_styles, animate_trace_indexes = zip(*[
                (trace_style, trace_index) for trace_index, trace_style in self._batch_style_commands.items()])
        else:
            animate_styles, animate_trace_indexes = {}, []

        animate_layout = self._batch_layout_commands

        # Send animate message to JS
        # --------------------------
        self._send_animate_msg(list(animate_styles), animate_layout, list(animate_trace_indexes), animation_opts)

        # Clear batched commands
        # ----------------------
        self._batch_layout_commands.clear()
        self._batch_style_commands.clear()

    def _send_animate_msg(self, styles, layout, trace_indexes, animation_opts):
        # print(styles, layout, trace_indexes, animation_opts)
        if not isinstance(trace_indexes, (list, tuple)):
            trace_indexes = [trace_indexes]

        # Add restyle message id
        restyle_msg_id = self._last_restyle_msg_id + 1
        for style in styles:
            style['_restyle_msg_id'] = restyle_msg_id

        self._last_restyle_msg_id = restyle_msg_id
        self._restyle_in_process = True

        # Add relayout message id
        relayout_msg_id = self._last_relayout_msg_id + 1
        layout['_relayout_msg_id'] = relayout_msg_id
        self._last_relayout_msg_id = relayout_msg_id
        self._relayout_in_process = True

        animate_msg = [{'data': styles,
                        'layout': layout,
                        'traces': trace_indexes},
                       animation_opts]

        if self._log_plotly_commands:
            print('Plotly.animate')
            pprint(animate_msg, indent=4)

        self._py2js_animate = animate_msg
        self._py2js_animate = None

    # Exports
    # -------
    def to_dict(self):
        data = deepcopy(self._data)
        layout = deepcopy(self._layout)
        return {'data': data, 'layout': layout}

    def save_html(self, filename, auto_open=False, responsive=False):
        data = self.to_dict()
        if responsive:
            if 'height' in data['layout']:
                data['layout'].pop('height')
            if 'width' in data['layout']:
                data['layout'].pop('width')
        else:
            # Assign width/height explicitly in case these were defaults
            data['layout']['height'] = self.layout.height
            data['layout']['width'] = self.layout.width

        plotlypy_plot(data, filename=filename, show_link=False, auto_open=auto_open, validate=False)

    def save_image(self, filename, image_type=None, scale_factor=2):
        """
        Save figure to a static image file

        Parameters
        ----------
        filename : str
            Image output file name
        image_type : str
            Image file type. One of: 'svg', 'png', 'pdf', or 'ps'. If not set, file type
            is inferred from the filename extension
        scale_factor : number
            (For png image type) Factor by which to increase the number of pixels in each
            dimension. A scale factor of 1 will result in a image with pixel dimensions
            (layout.width, layout.height).  A scale factor of 2 will result in an image
            with dimensions (2*layout.width, 2*layout.height), doubling image's DPI.
            (Default 2)
        """

        # Validate / infer image_type
        supported_image_types = ['svg', 'png', 'pdf', 'ps']
        cairo_image_types = ['png', 'pdf', 'ps']
        supported_types_csv = ', '.join(supported_image_types)

        if not image_type:
            # Infer image type from extension
            _, extension = os.path.splitext(filename)

            if not extension:
                raise ValueError('No image_type specified and file extension has no extension '
                                 'from which to infer an image type '
                                 'Supported image types are: {image_types}'
                                 .format(image_types=supported_types_csv))

            image_type = extension[1:]

        image_type = image_type.lower()
        if image_type not in supported_image_types:
            raise ValueError("Unsupported image type '{image_type}'\n"
                             "Supported image types are: {image_types}"
                             .format(image_type=image_type,
                                     image_types=supported_types_csv))

        # Validate cairo dependency
        if image_type in cairo_image_types:
            # Check whether we have cairosvg available
            try:
                import_module('cairosvg')
            except ModuleNotFoundError:
                raise ImportError('Exporting to {image_type} requires cairosvg'
                                  .format(image_type=image_type))

        # Validate scale_factor
        if not isinstance(scale_factor, numbers.Number) or scale_factor <= 0:
            raise ValueError('scale_factor must be a positive number.\n'
                             '    Received: {scale_factor}'.format(scale_factor=scale_factor))

        req_id = str(uuid.uuid1())

        # Register request
        self._svg_requests[req_id] = {'filename': filename,
                                      'image_type': image_type,
                                      'scale_factor': scale_factor}

        self._py2js_requestSvg = req_id
        self._py2js_requestSvg = None

    def _do_save_image(self, req_id, svg_uri):
        req_info = self._svg_requests.pop(req_id, None)
        if not req_info:
            return

        # Remove svg header
        if not svg_uri.startswith('data:image/svg+xml,'):
            raise ValueError('Invalid svg data URI')

        svg = svg_uri.replace('data:image/svg+xml,', '')

        # Unquote characters (e.g. '%3Csvg%20' -> '<svg ')
        svg_bytes = parse.unquote(svg).encode('utf-8')
        filename = req_info['filename']
        image_type = req_info['image_type']
        scale_factor = req_info['scale_factor']
        if image_type == 'svg':
            with open(filename, 'wb') as f:
                f.write(svg_bytes)
        else:
            # We already made sure cairosvg is available in save_image
            cairosvg = import_module('cairosvg')

            if image_type == 'png':
                cairosvg.svg2png(
                    bytestring=svg_bytes, write_to=filename, scale=scale_factor)
            elif image_type == 'pdf':
                cairosvg.svg2pdf(
                    bytestring=svg_bytes, write_to=filename)
            elif image_type == 'ps':
                cairosvg.svg2ps(
                    bytestring=svg_bytes, write_to=filename)

    # Custom Messages
    # ---------------
    def _handler_messages(self, widget, content, buffers):
        """Handle a msg from the front-end.
        """
        if content.get('event', '') == 'svg':
            req_id = content['req_id']
            svg_uri = content['svg_uri']
            self._do_save_image(req_id, svg_uri)

    # Static helpers
    # --------------
    @staticmethod
    def _str_to_dict_path(raw_key):

        if isinstance(raw_key, tuple):
            # Nothing to do
            return raw_key
        else:
            # Split string on periods. e.g. 'foo.bar[0]' -> ['foo', 'bar[0]']
            key_path = raw_key.split('.')

            # Split out bracket indexes. e.g. ['foo', 'bar[0]'] -> ['foo', 'bar', '0']
            bracket_re = re.compile('(.*)\[(\d+)\]')
            key_path2 = []
            for key in key_path:
                match = bracket_re.fullmatch(key)
                if match:
                    key_path2.extend(match.groups())
                else:
                    key_path2.append(key)

            # Convert elements to ints if possible. e.g. e.g. ['foo', 'bar', '0'] -> ['foo', 'bar', 0]
            for i in range(len(key_path2)):
                try:
                    key_path2[i] = int(key_path2[i])
                except ValueError as _:
                    pass

            return tuple(key_path2)

    @staticmethod
    def _is_object_list(v):
        return isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict)

    @staticmethod
    def remove_overlapping_props(input_data, delta_data, prop_path=()):
        """
        Remove properties in data that are also into delta. Do so recursively.

        Except, never remove uid from input_data

        Parameters
        ----------
        data :
        delta :

        Returns
        -------
        List of removed property path tuples
        """
        removed = []
        if isinstance(input_data, dict):
            assert isinstance(delta_data, dict)

            for p, delta_val in delta_data.items():
                if isinstance(delta_val, dict) or BaseFigureWidget._is_object_list(delta_val):
                    if p in input_data:
                        input_val = input_data[p]
                        removed.extend(
                            BaseFigureWidget.remove_overlapping_props(
                                input_val,
                                delta_val,
                                prop_path + (p,)))
                elif p in input_data and p != 'uid':
                    input_data.pop(p)
                    removed.append(prop_path + (p,))

        elif isinstance(input_data, list):
            assert isinstance(delta_data, list)

            for i, delta_val in enumerate(delta_data):
                if i >= len(input_data):
                    break

                input_val = input_data[i]
                if input_val is not None and isinstance(delta_val, dict) or BaseFigureWidget._is_object_list(delta_val):
                    removed.extend(
                        BaseFigureWidget.remove_overlapping_props(
                            input_val,
                            delta_val,
                            prop_path + (i,)))

        return removed

    @staticmethod
    def transform_data(to_data, from_data, should_remove=True, relayout_path=()):
        """
        Transform to_data into from_data and return relayout style description of transformation

        Parameters
        ----------
        to_data :
        from_data :

        Returns
        -------

        """
        relayout_terms = {}
        if isinstance(to_data, dict):
            if not isinstance(from_data, dict):
                raise ValueError('Mismatched data types: to_data: {to_dict} {from_data}'.format(
                    to_dict=to_data, from_data=from_data))

            # Handle addition / modification of terms
            for from_prop, from_val in from_data.items():
                if isinstance(from_val, dict) or BaseFigureWidget._is_object_list(from_val):
                    if from_prop not in to_data:
                        to_data[from_prop] = {} if isinstance(from_val, dict) else []

                    input_val = to_data[from_prop]
                    relayout_terms.update(
                        BaseFigureWidget.transform_data(
                            input_val,
                            from_val,
                            should_remove=should_remove,
                            relayout_path=relayout_path + (from_prop,)))
                else:
                    if from_prop not in to_data or not BasePlotlyType._vals_equal(to_data[from_prop], from_val):
                        # if from_prop in to_data:
                        #     print(f'to_data[from_prop] != from_val -- {to_data}[{from_prop}] != {from_val}:')
                        to_data[from_prop] = from_val
                        relayout_terms[relayout_path + (from_prop,)] = from_val

            # Handle removal of terms
            if should_remove:
                for remove_prop in set(to_data.keys()).difference(set(from_data.keys())):
                    to_data.pop(remove_prop)

        elif isinstance(to_data, list):
            if not isinstance(from_data, list):
                raise ValueError('Mismatched data types: to_data: {to_data} {from_data}'.format(
                    to_data=to_data, from_data=from_data))

            for i, from_val in enumerate(from_data):
                if i >= len(to_data):
                    to_data.append(None)

                input_val = to_data[i]
                if input_val is not None and isinstance(from_val, dict) or BaseFigureWidget._is_object_list(from_val):
                    relayout_terms.update(
                        BaseFigureWidget.transform_data(
                            input_val,
                            from_val,
                            should_remove=should_remove,
                            relayout_path=relayout_path + (i,)))
                else:
                    if not BasePlotlyType._vals_equal(to_data[i], from_val):
                        to_data[i] = from_val
                        relayout_terms[relayout_path + (i,)] = from_val

        return relayout_terms


class BasePlotlyType:
    _validators = None

    # Defaults to help mocking
    def __init__(self, name, **kwargs):

        self._name = name
        self._raise_on_invalid_property_error(**kwargs)
        self._validators = {}
        self._compound_props = {}
        self._orphan_props = {}  # properties dict for use while object has no parent
        self._parent = None
        self._change_callbacks = {}  # type: typ.Dict[typ.Tuple, typ.Callable]

    @property
    def name(self):
        return self._name

    @property
    def _parent_path(self) -> str:
        raise NotImplementedError

    @property
    def _prop_descriptions(self) -> str:
        raise NotImplementedError

    def __setattr__(self, prop, value):
        if prop.startswith('_') or hasattr(self, prop):
            # Let known properties and private properties through
            super().__setattr__(prop, value)
        else:
            # Raise error on unknown public properties
            self._raise_on_invalid_property_error(**{prop: value})

    def _raise_on_invalid_property_error(self, **kwargs):
        invalid_props = list(kwargs.keys())
        if invalid_props:
            if len(invalid_props) == 1:
                prop_str = 'property'
                invalid_str = repr(invalid_props[0])
            else:
                prop_str = 'properties'
                invalid_str = repr(invalid_props)

            if self._parent_path:
                full_prop_name = self._parent_path + '.' + self.name
            else:
                full_prop_name = self.name

            raise ValueError("Invalid {prop_str} specified for {full_prop_name}: {invalid_str}\n\n"
                             "    Valid properties:\n"
                             "{prop_descriptions}"
                             .format(prop_str=prop_str,
                                     full_prop_name=full_prop_name,
                                     invalid_str=invalid_str,
                                     prop_descriptions=self._prop_descriptions))

    @property
    def _props(self):
        if self.parent is None:
            # Use orphan data
            return self._orphan_props
        else:
            # Get data from parent's dict
            return self.parent._get_child_props(self)

    def _init_props(self):
        # Ensure that _data is initialized.
        if self._props is not None:
            pass
        else:
            self._parent._init_child_props(self)

    def _init_child_props(self, child):
        self.parent._init_child_props(self)
        self_props = self.parent._get_child_props(self)

        child_or_children = self._compound_props[child.name]
        if child is child_or_children:
            if child.name not in self_props:
                self_props[child.name] = {}
        elif isinstance(child_or_children, (list, tuple)):
            child_ind = child_or_children.index(child)
            if child.name not in self_props:
                # Initialize list
                self_props[child.name] = []

            # Make sure list is long enough for child
            child_list = self_props[child.name]
            while(len(child_list) <= child_ind):
                child_list.append({})

    def _get_child_props(self, child):
        self_props = self.parent._get_child_props(self)
        if self_props is None:
            return None
        else:
            child_or_children = self._compound_props[child.name]
            if child is child_or_children:
                return self_props.get(child.name, None)
            elif isinstance(child_or_children, (list, tuple)):
                child_ind = child_or_children.index(child)
                children_props = self_props.get(child.name, None)
                return children_props[child_ind] \
                    if children_props is not None and len(children_props) > child_ind \
                    else None
            else:
                ValueError('Unexpected child: %s' % child_or_children)

    @property
    def _prop_defaults(self):
        if self.parent is None:
            return None
        else:
            return self.parent._get_child_prop_defaults(self)

    def _get_child_prop_defaults(self, child):
        if self.parent is None:
            return None

        self_prop_defaults = self.parent._get_child_prop_defaults(self)
        if self_prop_defaults is None:
            return None
        else:
            child_or_children = self._compound_props[child.name]
            if child is child_or_children:
                return self_prop_defaults.get(child.name, None)
            elif isinstance(child_or_children, (list, tuple)):
                child_ind = child_or_children.index(child)
                children_props = self_prop_defaults.get(child.name, None)
                return children_props[child_ind] if children_props is not None else None
            else:
                ValueError('Unexpected child: %s' % child_or_children)

    @property
    def parent(self):
        return self._parent

    def __getitem__(self, prop):
        if isinstance(prop, tuple):
            res = self
            for p in prop:
                res = res[p]

            return res
        else:
            if prop not in self._validators:
                raise KeyError(prop)

            if prop in self._compound_props:
                return self._compound_props[prop]
            elif self._props is not None and prop in self._props:
                return self._props[prop]
            elif self._prop_defaults is not None:
                return self._prop_defaults.get(prop, None)
            else:
                return None

    def __contains__(self, prop):
        return prop in self._validators

    def __setitem__(self, key, value):
        if key not in self._validators:
            raise KeyError(key)

        validator = self._validators[key]

        if isinstance(validator, CompoundValidator):
            self._set_compound_prop(key, value)
        elif isinstance(validator, CompoundArrayValidator):
            self._set_array_prop(key, value)
        else:
            # Simple property
            self._set_prop(key, value)

    @property
    def _in_batch_mode(self):
        return self.parent and self.parent._in_batch_mode

    @staticmethod
    def _vals_equal(v1, v2):
        if isinstance(v1, np.ndarray) or isinstance(v2, np.ndarray):
            return np.array_equal(v1, v2)
        else:
            return v1 == v2

    def _set_prop(self, prop, val):
        if val is Undefined:
            # Do nothing
            return

        validator = self._validators.get(prop)
        val = validator.validate_coerce(val)

        if val is None:
            # Check if we should send null update
            if self._props and prop in self._props:
                if not self._in_batch_mode:
                    self._props.pop(prop)
                self._send_update(prop, val)
        else:
            self._init_props()
            if prop not in self._props or not BasePlotlyType._vals_equal(self._props[prop], val):
                if not self._in_batch_mode:
                    self._props[prop] = val
                self._send_update(prop, val)

    def _set_compound_prop(self, prop, val):
        if val is Undefined:
            # Do nothing
            return

        # Validate coerce new value
        validator = self._validators.get(prop)
        val = validator.validate_coerce(val)  # type: BasePlotlyType

        # Grab deep copies of current and new states
        curr_val = self._compound_props.get(prop, None)
        if curr_val is not None:
            curr_dict_val = deepcopy(curr_val._props)
        else:
            curr_dict_val = None

        if val is not None:
            new_dict_val = deepcopy(val._props)
        else:
            new_dict_val = None

        # Update data dict
        if not self._in_batch_mode:
            if not new_dict_val:
                if prop in self._props:
                    self._props.pop(prop)
            else:
                self._init_props()
                self._props[prop] = new_dict_val

        # Send update if there was a change in value
        if not BasePlotlyType._vals_equal(curr_dict_val, new_dict_val):
            self._send_update(prop, new_dict_val)

        # Reparent new value and clear orphan data
        val._parent = self
        val._orphan_props.clear()

        # Reparent old value and update orphan data
        if curr_val is not None and curr_val is not val:
            if curr_dict_val is not None:
                curr_val._orphan_props.update(curr_dict_val)
            curr_val._parent = None

        self._compound_props[prop] = val
        return val

    def _set_array_prop(self, prop, val):
        if val is Undefined:
            # Do nothing
            return

        # Validate coerce new value
        validator = self._validators.get(prop)
        val = validator.validate_coerce(val)  # type: tuple

        # Update data dict
        curr_val = self._compound_props.get(prop, None)
        if curr_val is not None:
            curr_dict_vals = [deepcopy(cv._props) for cv in curr_val]
        else:
            curr_dict_vals = None

        if val is not None:
            new_dict_vals = [deepcopy(nv._props) for nv in val]
        else:
            new_dict_vals = None

        # Update data dict
        if not self._in_batch_mode:
            if not new_dict_vals:
                if prop in self._props:
                    self._props.pop(prop)
            else:
                self._init_props()
                self._props[prop] = new_dict_vals

        # Send update if there was a change in value
        if not BasePlotlyType._vals_equal(curr_dict_vals, new_dict_vals):
            self._send_update(prop, new_dict_vals)

        # Reparent new values and clear orphan data
        if val is not None:
            for v in val:
                v._orphan_props.clear()
                v._parent = self

        # Reparent
        if curr_val is not None:
            for cv, cv_dict in zip(curr_val, curr_dict_vals):
                if cv_dict is not None:
                    cv._orphan_props.update(cv_dict)
                cv._parent = None
        self._compound_props[prop] = val
        return val

    def _send_update(self, prop, val):
        raise NotImplementedError()

    def _update_child(self, child, prop, val):
        child_prop_val = getattr(self, child.name)
        if isinstance(child_prop_val, (list, tuple)):
            child_ind = child_prop_val.index(child)
            obj_path = '{child_name}.{child_ind}.{prop}'.format(
                child_name=child.name,
                child_ind=child_ind,
                prop=prop)
        else:
            obj_path = '{child_name}.{prop}'.format(child_name=child.name, prop=prop)

        self._send_update(obj_path, val)

    def _restyle_child(self, child, prop, val):
        self._update_child(child, prop, val)

    def _relayout_child(self, child, prop, val):
        self._update_child(child, prop, val)

    # Callbacks
    # ---------
    def _dispatch_change_callbacks(self, changed_paths):
        # print(f'Change callback: {self.prop_name} - {changed_paths}')
        changed_paths = set(changed_paths)
        # pprint(changed_paths)
        for callback_paths, callback in self._change_callbacks.items():
            # pprint(set(callback_paths))
            common_paths = changed_paths.intersection(set(callback_paths))
            if common_paths:
                # Invoke callback
                callback_args = [self[cb_path] for cb_path in callback_paths]
                callback(self, *callback_args)

    def on_change(self, callback, *args):
        """
        Register callback function to be called with a properties or subproperties of this object are modified

        Parameters
        ----------
        callback : function
            Function that accepts 1 + len(args) parameters. First parameter is this object. Second throug last
            parameters are the values referenced by args
        args : str or tuple(str)
            Property name (for direct properties) or tuple of property names / indices (for sub properties). Callback
            will be invoked whenever ANY of these properties is modified. Furthermore. The callback will only be
            invoked once even if multiple properties are modified during the same restyle operation.

        Returns
        -------

        """

        if len(args) == 0:
            raise ValueError('At least one property/subproperty must be specified')

        # TODO: Validate that args valid properties / subproperties
        validated_args = tuple([a if isinstance(a, tuple) else (a,) for a in args])

        # TODO: add append arg and store list of callbacks
        self._change_callbacks[validated_args] = callback


class BaseLayoutHierarchyType(BasePlotlyType):

    # _send_relayout analogous to _send_restyle above
    def __init__(self, name, **kwargs):
        super().__init__(name, **kwargs)

    def _send_update(self, prop, val):
        if self.parent:
            self.parent._relayout_child(self, prop, val)


class BaseLayoutType(BaseLayoutHierarchyType):
    _subplotid_prop_names = ['xaxis', 'yaxis', 'geo', 'ternary', 'scene']
    _subplotid_validators = {'xaxis': XaxisValidator,
                             'yaxis': YaxisValidator,
                             'geo': GeoValidator,
                             'ternary': TernaryValidator,
                             'scene': SceneValidator}

    _subplotid_prop_re = re.compile('(' + '|'.join(_subplotid_prop_names) + ')(\d+)')

    def __init__(self, name, **kwargs):
        # Compute invalid kwargs. Pass to parent for error message
        invalid_kwargs = {k: v for k, v in kwargs.items()
                          if not self._subplotid_prop_re.fullmatch(k)}
        super().__init__(name, **invalid_kwargs)
        self._subplotid_props = {}
        for prop, value in kwargs.items():
            self._set_subplotid_prop(prop, value)

    def _set_subplotid_prop(self, prop, value):
        # We already tested for match in constructor
        match = self._subplotid_prop_re.fullmatch(prop)
        subplot_prop = match.group(1)
        suffix_digit = int(match.group(2))
        if suffix_digit in [0, 1]:
            raise TypeError('Subplot properties may only be suffixed by an integer > 1\n'
                            'Received {k}'.format(k=prop))

        # Add validator
        if prop not in self._validators:
            validator = self._subplotid_validators[subplot_prop](prop_name=prop)
            self._validators[prop] = validator

        # Import value
        self._subplotid_props[prop] = self._set_compound_prop(prop, value)

    def __getattr__(self, item):
        # Check for subplot access (e.g. xaxis2)
        # Validate then call self._get_prop(item)
        if item in self._subplotid_props:
            return self._subplotid_props[item]

        raise AttributeError("'Layout' object has no attribute '{item}'".format(item=item))

    def __setattr__(self, prop, value):
        # Check for subplot assignment (e.g. xaxis2)
        # Call _set_compound_prop with the xaxis validator
        match = self._subplotid_prop_re.fullmatch(prop)
        if match is None:
            # Try setting as ordinary property
            super().__setattr__(prop, value)
        else:
            self._set_subplotid_prop(prop, value)

    def __dir__(self):
        # Include any active subplot values (xaxis2 etc.)
        return super().__dir__() + list(self._subplotid_props.keys())


class BaseTraceHierarchyType(BasePlotlyType):

    def __init__(self, name, **kwargs):
        super().__init__(name, **kwargs)

    def _send_update(self, prop, val):
        if self.parent:
            self.parent._restyle_child(self, prop, val)


class BaseTraceType(BaseTraceHierarchyType):
    def __init__(self, name, **kwargs):
        super().__init__(name, **kwargs)

        self._hover_callbacks = []
        self._unhover_callbacks = []
        self._click_callbacks = []
        self._select_callbacks = []

    # uid
    # ---
    @property
    def uid(self) -> str:
        raise NotImplementedError

    @uid.setter
    def uid(self, val):
        raise NotImplementedError

    # Hover
    # -----
    def on_hover(self,
                 callback: typ.Callable[['BaseTraceType', Points, InputState], None],
                 append=False):
        """
        Register callback to be called when the user hovers over a point from this trace

        Parameters
        ----------
        callback
            Callable that accepts 3 arguments

            - This trace
            - Points object
            - InputState object

        append :

        Returns
        -------
        None
        """
        if not append:
            self._hover_callbacks.clear()

        if callback:
            self._hover_callbacks.append(callback)

    def _dispatch_on_hover(self, points: Points, state: InputState):
        for callback in self._hover_callbacks:
            callback(self, points, state)

    # Unhover
    # -------
    def on_unhover(self, callback: typ.Callable[['BaseTraceType', Points, InputState], None], append=False):
        if not append:
            self._unhover_callbacks.clear()

        if callback:
            self._unhover_callbacks.append(callback)

    def _dispatch_on_unhover(self, points: Points, state: InputState):
        for callback in self._unhover_callbacks:
            callback(self, points, state)

    # Click
    # -----
    def on_click(self, callback: typ.Callable[['BaseTraceType', Points, InputState], None], append=False):
        if not append:
            self._click_callbacks.clear()
        if callback:
            self._click_callbacks.append(callback)

    def _dispatch_on_click(self, points: Points, state: InputState):
        for callback in self._click_callbacks:
            callback(self, points, state)

    # Select
    # ------
    def on_selected(self,
                    callback: typ.Callable[['BaseTraceType', Points, typ.Union[BoxSelector, LassoSelector]], None],
                    append=False):
        if not append:
            self._select_callbacks.clear()

        if callback:
            self._select_callbacks.append(callback)

    def _dispatch_on_selected(self, points: Points, selector: typ.Union[BoxSelector, LassoSelector]):
        for callback in self._select_callbacks:
            callback(self, points, selector)
