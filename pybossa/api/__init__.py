# -*- coding: utf8 -*-
# This file is part of PyBossa.
#
# Copyright (C) 2015 SciFabric LTD.
#
# PyBossa is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PyBossa is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with PyBossa.  If not, see <http://www.gnu.org/licenses/>.
"""
PyBossa api module for exposing domain objects via an API.

This package adds GET, POST, PUT and DELETE methods for:
    * projects,
    * categories,
    * tasks,
    * task_runs,
    * users,
    * global_stats,
    * vmcp

"""

import json

from bson import json_util
from flask import Blueprint, request, abort, Response, make_response
from flask.ext.login import current_user
from pybossa.mongo import task_run_mongo
from pybossa.areacalculator import area_calculator
from werkzeug.exceptions import NotFound
from pybossa.util import jsonpify, crossdomain, get_user_id_or_ip
import pybossa.model as model
from pybossa.core import csrf, ratelimits, sentinel
from pybossa.ratelimit import ratelimit
from pybossa.cache.projects import n_tasks
from pybossa.cache import users as cached_users
import pybossa.sched as sched
from pybossa.error import ErrorStatus
from global_stats import GlobalStatsAPI
from task import TaskAPI
from task_run import TaskRunAPI
from app import AppAPI
from project import ProjectAPI
from category import CategoryAPI
from vmcp import VmcpAPI
from user import UserAPI
from user_score import UserScoreAPI
from token import TokenAPI
from result import ResultAPI
from pybossa.core import project_repo, task_repo
from pybossa.contributions_guard import ContributionsGuard
from pybossa.cache import memoize, THIRTY_SECONDS

blueprint = Blueprint('api', __name__)

cors_headers = ['Content-Type', 'Authorization']

error = ErrorStatus()


@blueprint.route('/')
@crossdomain(origin='*', headers=cors_headers)
@ratelimit(limit=ratelimits.get('LIMIT'), per=ratelimits.get('PER'))
def index():  # pragma: no cover
    """Return dummy text for welcome page."""
    return 'The PyBossa API'


def register_api(view, endpoint, url, pk='id', pk_type='int'):
    """Register API endpoints.

    Registers new end points for the API using classes.

    """
    view_func = view.as_view(endpoint)
    csrf.exempt(view_func)
    blueprint.add_url_rule(url,
                           view_func=view_func,
                           defaults={pk: None},
                           methods=['GET', 'OPTIONS'])
    blueprint.add_url_rule(url,
                           view_func=view_func,
                           methods=['POST', 'OPTIONS'])
    blueprint.add_url_rule('%s/<%s:%s>' % (url, pk_type, pk),
                           view_func=view_func,
                           methods=['GET', 'PUT', 'DELETE', 'OPTIONS'])

register_api(AppAPI, 'api_app', '/app', pk='oid', pk_type='int')
register_api(ProjectAPI, 'api_project', '/project', pk='oid', pk_type='int')
register_api(CategoryAPI, 'api_category', '/category', pk='oid', pk_type='int')
register_api(TaskAPI, 'api_task', '/task', pk='oid', pk_type='int')
register_api(TaskRunAPI, 'api_taskrun', '/taskrun', pk='oid', pk_type='int')
register_api(ResultAPI, 'api_result', '/result', pk='oid', pk_type='int')
register_api(UserAPI, 'api_user', '/user', pk='oid', pk_type='int')
register_api(UserScoreAPI, 'api_user_score', '/userscore', pk='oid', pk_type='int')
register_api(GlobalStatsAPI, 'api_globalstats', '/globalstats',
             pk='oid', pk_type='int')
register_api(VmcpAPI, 'api_vmcp', '/vmcp', pk='oid', pk_type='int')
register_api(TokenAPI, 'api_token', '/token', pk='token', pk_type='string')


@jsonpify
@blueprint.route('/app/<project_id>/newtask')
@blueprint.route('/project/<project_id>/newtask')
@crossdomain(origin='*', headers=cors_headers)
@ratelimit(limit=ratelimits.get('LIMIT'), per=ratelimits.get('PER'))
def new_task(project_id):
    """Return a new task for a project."""
    # Check if the request has an arg:
    try:
        task = _retrieve_new_task(project_id)
        # If there is a task for the user, return it
        if task is not None:
            guard = ContributionsGuard(sentinel.master)
            guard.stamp(task, get_user_id_or_ip())
            response = make_response(json.dumps(task.dictize()))
            response.mimetype = "application/json"
            return response
        return Response(json.dumps({}), mimetype="application/json")
    except Exception as e:
        return error.format_exception(e, target='project', action='GET')


def _retrieve_new_task(project_id):
    project = project_repo.get(project_id)
    if project is None:
        raise NotFound
    if not project.allow_anonymous_contributors and current_user.is_anonymous():
        info = dict(
            error="This project does not allow anonymous contributors")
        error = model.task.Task(info=info)
        return error
    if request.args.get('offset'):
        offset = int(request.args.get('offset'))
    else:
        offset = 0
    user_id = None if current_user.is_anonymous() else current_user.id
    user_ip = request.remote_addr if current_user.is_anonymous() else None
    task = sched.new_task(project_id, project.info.get('sched'),
                          user_id,
                          user_ip,
                          offset)
    return task




@jsonpify
@blueprint.route('/project/<int:project_id>/leaderboard/limit/<int:limit>')
@crossdomain(origin='*', headers=cors_headers)
def get_top_project_contributors(project_id, limit):
    user_id = None if current_user.is_anonymous() else current_user.id
    top_contributors = cached_users.get_leaderboard(limit, user_id, project_id, True)
    return Response(json.dumps(top_contributors), 200, mimetype='application/json')


@jsonpify
@blueprint.route('/project/<project_parent_short_name>/<user_id_or_ip>/progress.json')
@crossdomain(origin='*', headers=cors_headers)
@memoize(timeout=THIRTY_SECONDS)
def get_km_square(project_parent_short_name, user_id_or_ip):
    results = area_calculator.get_square_km_all_volunteers(project_parent_short_name, user_id_or_ip)
    return Response(results, 200,
                    mimetype='application/json')

@jsonpify
@blueprint.route('/project/<project_id>/task_count.json')
@crossdomain(origin='*', headers=cors_headers)
def user_task_count(project_id=None):
    if project_id:
        try:
            user_task_count = {}
            if current_user.is_authenticated():
                user_task_count['tasks'] = task_repo.count_task_runs_with(project_id=project_id,  user_id=current_user.id)
            if current_user.is_anonymous():
                user_task_count['tasks'] = task_repo.count_task_runs_with(project_id=project_id, user_ip=request.remote_addr)
            results = json_util.dumps(user_task_count)
            return Response(results, 200, mimetype='application/json')
        except Exception as e:
            return e.message
    else:
        return Response(json.dumps([]), mimetype="application/json")


@jsonpify
@blueprint.route('/project/<project_short_name>/validated/results.json')
@blueprint.route('/project/<project_short_name>/validated/<int:task_id>/results.json')
@crossdomain(origin='*', headers=cors_headers)
@ratelimit(limit=ratelimits.get('LIMIT'), per=ratelimits.get('PER'))
def _get_tile_results(project_short_name=None, task_id=None):
    #FIXME: Should only be accessible by admin
    '''
    try:
        if project_short_name and task_id:
            results = task_run_mongo.consolidate_redundancy(project_short_name, task_id)
        else:
            if task_id:
                results = task_run_mongo.consolidate_redundancy(task_id)
            if project_short_name:
                results = task_run_mongo.consolidate_redundancy(project_short_name)
        result_dumps = json_util.dumps(results)
        return Response(result_dumps, 200,
                        mimetype='application/json')
    except Exception as e:
        return e.message
    '''
    return Response(json_util.dumps({}), 200, mimetype='application/json')


@jsonpify
@blueprint.route('/app/<short_name>/userprogress')
@blueprint.route('/project/<short_name>/userprogress')
@blueprint.route('/app/<int:project_id>/userprogress')
@blueprint.route('/project/<int:project_id>/userprogress')
@crossdomain(origin='*', headers=cors_headers)
@ratelimit(limit=ratelimits.get('LIMIT'), per=ratelimits.get('PER'))
def user_progress(project_id=None, short_name=None):
    """API endpoint for user progress.

    Return a JSON object with two fields regarding the tasks for the user:
        { 'done': 10,
          'total: 100
        }
       This will mean that the user has done a 10% of the available tasks for
       him

    """
    if project_id or short_name:
        if short_name:
            project = project_repo.get_by_shortname(short_name)
        elif project_id:
            project = project_repo.get(project_id)

        if project:
            # For now, keep this version, but wait until redis cache is used here for task_runs too
            query_attrs = dict(project_id=project.id)
            if current_user.is_anonymous():
                query_attrs['user_ip'] = request.remote_addr or '127.0.0.1'
            else:
                query_attrs['user_id'] = current_user.id
            taskrun_count = task_repo.count_task_runs_with(**query_attrs)
            tmp = dict(done=taskrun_count, total=n_tasks(project.id))
            return Response(json.dumps(tmp), mimetype="application/json")
        else:
            return abort(404)
    else:  # pragma: no cover
        return abort(404)
