import copy
import os
import shutil
import sys
import time
from io import BytesIO
import mindsdb

from dateutil.parser import parse as parse_datetime
from flask import request, send_file
from flask_restx import Resource, abort

from mindsdb_server.api.http.namespaces.configs.predictors import ns_conf
from mindsdb_server.api.http.namespaces.entitites.predictor_metadata import (
    predictor_metadata,
    predictor_query_params,
    upload_predictor_params,
    put_predictor_params
)
from mindsdb_server.api.http.namespaces.entitites.predictor_status import predictor_status
from mindsdb_server.api.http.shared_ressources import get_shared
from mindsdb_server.interfaces.datastore.datastore import DataStore
from mindsdb_server.interfaces.native.mindsdb import MindsdbNative
from mindsdb_server.utilities import config

app, api = get_shared()
model_swapping_map = {}

default_store = DataStore(config)
mindsdb_native = MindsdbNative(config)

def debug_pkey_type(model, keys=None, reset_keyes=True, type_to_check=list, append_key=True):
    if type(model) != dict:
        return
    for k in model:
        if reset_keyes:
            keys = []
        if type(model[k]) == dict:
            keys.append(k)
            debug_pkey_type(model[k], copy.deepcopy(keys), reset_keyes=False)
        if type(model[k]) == type_to_check:
            print(f'They key {keys}->{k} has type list')
        if type(model[k]) == list:
            for item in model[k]:
                debug_pkey_type(item, copy.deepcopy(keys), reset_keyes=False)


def preparse_results(results, format_flag='explain'):
    response_arr = []
    for res in results:
        if format_flag == 'explain':
            response_arr.append(res.explain())
        elif format_flag == 'epitomize':
            response_arr.append(res.epitomize())
        elif format_flag == 'new_explain':
            response_arr.append(res.explanation)
        else:
            response_arr.append(res.explain())

    if len(response_arr) > 0:
        return response_arr
    else:
        abort(400, "")


@ns_conf.route('/')
class PredictorList(Resource):
    @ns_conf.doc('list_predictors')
    @ns_conf.marshal_list_with(predictor_status, skip_none=True)
    def get(self):
        global mindsdb_native
        '''List all predictors'''

        return mindsdb_native.get_models()


@ns_conf.route('/<name>')
@ns_conf.param('name', 'The predictor identifier')
@ns_conf.response(404, 'predictor not found')
class Predictor(Resource):
    @ns_conf.doc('get_predictor')
    @ns_conf.marshal_with(predictor_metadata, skip_none=True)
    def get(self, name):
        global mindsdb_native

        try:
            model = mindsdb_native.get_model_data(name)
        except Exception as e:
            abort(404, "")

        for k in ['train_end_at', 'updated_at', 'created_at']:
            if k in model and model[k] is not None:
                model[k] = parse_datetime(model[k])

        return model

    @ns_conf.doc('delete_predictor')
    def delete(self, name):
        '''Remove predictor'''
        global mindsdb_native
        mindsdb_native.delete_model(name)
        return '', 200

    @ns_conf.doc('put_predictor', params=put_predictor_params)
    def put(self, name):
        '''Learning new predictor'''
        global model_swapping_map
        global mindsdb_native

        data = request.json
        to_predict = data.get('to_predict')

        try:
            kwargs = data.get('kwargs')
        except:
            kwargs = None

        if type(kwargs) != type({}):
            kwargs = {}

        if 'stop_training_in_x_seconds' not in kwargs:
            kwargs['stop_training_in_x_seconds'] = 2

        if 'equal_accuracy_for_all_output_categories' not in kwargs:
            kwargs['equal_accuracy_for_all_output_categories'] = True

        if 'sample_margin_of_error' not in kwargs:
            kwargs['sample_margin_of_error'] = 0.005

        if 'unstable_parameters_dict' not in kwargs:
            kwargs['unstable_parameters_dict'] = {}

        if 'use_selfaware_model' not in kwargs['unstable_parameters_dict']:
            kwargs['unstable_parameters_dict']['use_selfaware_model'] = False

        try:
            retrain = data.get('retrain')
            if retrain in ('true', 'True'):
                retrain = True
            else:
                retrain = False
        except:
            retrain = None

        ds_name = data.get('data_source_name') if data.get('data_source_name') is not None else data.get('from_data')
        from_data = default_store.get_datasource_obj(ds_name)

        if retrain is True:
            original_name = name
            name = name + '_retrained'

        mindsdb_native.learn(name, from_data, to_predict, kwargs)

        if retrain is True:
            try:
                model_swapping_map[original_name] = True
                mindsdb_native.delete_model(original_name)
                mindsdb_native.rename_model(name, original_name)
                model_swapping_map[original_name] = False
            except:
                model_swapping_map[original_name] = False

        return '', 200


@ns_conf.route('/<name>/columns')
@ns_conf.param('name', 'The predictor identifier')
class PredictorColumns(Resource):
    @ns_conf.doc('get_predictor_columns')
    def get(self, name):
        '''List of predictors colums'''
        global mindsdb_native
        try:
            model = mindsdb_native.get_model_data(name)
        except Exception:
            abort(404, 'Invalid predictor name')

        columns = []
        for array, is_target_array in [(model['data_analysis']['target_columns_metadata'], True),
                                       (model['data_analysis']['input_columns_metadata'], False)]:
            for col_data in array:
                column = {
                    'name': col_data['column_name'],
                    'data_type': col_data['data_type'].lower(),
                    'is_target_column': is_target_array
                }
                if column['data_type'] == 'categorical':
                    column['distribution'] = col_data["data_distribution"]["data_histogram"]["x"]
                columns.append(column)

        return columns, 200


@ns_conf.route('/<name>/predict')
@ns_conf.param('name', 'The predictor identifier')
class PredictorPredict(Resource):
    @ns_conf.doc('post_predictor_predict', params=predictor_query_params)
    def post(self, name):
        '''Queries predictor'''
        global model_swapping_map
        global mindsdb_native

        data = request.json

        when = data.get('when') or {}
        try:
            format_flag = data.get('format_flag')
        except:
            format_flag = 'explain'

        try:
            kwargs = data.get('kwargs')
        except:
            kwargs = {}

        if type(kwargs) != type({}):
            kwargs = {}

        # Not the fanciest semaphor, but should work since restplus is multi-threaded and this condition should rarely be reached
        while name in model_swapping_map and model_swapping_map[name] is True:
            time.sleep(1)

        results = mindsdb_native.predict(name, when=when, **kwargs)
        # return '', 500
        return preparse_results(results, format_flag)


@ns_conf.route('/<name>/predict_datasource')
@ns_conf.param('name', 'The predictor identifier')
class PredictorPredictFromDataSource(Resource):
    @ns_conf.doc('post_predictor_predict', params=predictor_query_params)
    def post(self, name):
        global model_swapping_map
        global mindsdb_native

        data = request.json

        from_data = default_store.get_datasource_obj(data.get('data_source_name'))

        try:
            format_flag = data.get('format_flag')
        except:
            format_flag = 'explain'

        try:
            kwargs = data.get('kwargs')
        except:
            kwargs = {}

        if type(kwargs) != type({}):
            kwargs = {}

        if from_data is None:
            from_data = data.get('from_data')
        if from_data is None:
            from_data = data.get('when_data')
        if from_data is None:
            abort(400, 'No valid datasource given')

        # Not the fanciest semaphor, but should work since restplus is multi-threaded and this condition should rarely be reached
        while name in model_swapping_map and model_swapping_map[name] is True:
            time.sleep(1)

        results = mindsdb_native.predict(name, when_data=from_data, **kwargs)
        return preparse_results(results, format_flag)


@ns_conf.route('/upload')
class PredictorUpload(Resource):
    @ns_conf.doc('predictor_query', params=upload_predictor_params)
    def post(self):
        '''Upload existing predictor'''
        global mindsdb_native
        predictor_file = request.files['file']
        # @TODO: Figure out how to remove
        fpath = os.path.join(mindsdb.CONFIG.MINDSDB_TEMP_PATH, 'new.zip')
        with open(fpath, 'wb') as f:
            f.write(predictor_file.read())

        mindsdb_native.load_model(fpath)
        try:
            os.remove(fpath)
        except Exception:
            pass

        return '', 200


@ns_conf.route('/<name>/download')
@ns_conf.param('name', 'The predictor identifier')
class PredictorDownload(Resource):
    @ns_conf.doc('get_predictor_download')
    def get(self, name):
        '''Export predictor to file'''
        global mindsdb_native
        mindsdb_native.export_model(name)
        fname = name + '.zip'
        original_file = os.path.join(fname)
        # @TODO: Figure out how to remove
        fpath = os.path.join(mindsdb.CONFIG.MINDSDB_TEMP_PATH, fname)
        shutil.move(original_file, fpath)

        with open(fpath, 'rb') as f:
            data = BytesIO(f.read())

        try:
            os.remove(fpath)
        except Exception as e:
            pass

        return send_file(
            data,
            mimetype='application/zip',
            attachment_filename=fname,
            as_attachment=True
        )

@ns_conf.route('/<name>/rename')
@ns_conf.param('name', 'The predictor identifier')
class PredictorDownload(Resource):
    @ns_conf.doc('get_predictor_download')
    def get(self, name):
        '''Export predictor to file'''
        global mindsdb_native

        try:
            new_name = request.args.get('new_name')
            mindsdb_native.rename_model(name, new_name)
        except Exception as e:
            return str(e), 400

        return f'Renamed model to {new_name}', 200