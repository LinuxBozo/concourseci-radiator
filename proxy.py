import json
import os
from flask import Flask, Response, request, g
import logging
import requests
import hashlib

# key = connfiguration variable to be loaded from the environment
# value = The default to use if env var is not present
CONFIG_KEYS = {
    'CONCOURSE_URL': 'http://concourse.example.com',
    'CONCOURSE_USERNAME': 'admin',
    'CONCOURSE_PASSWORD': 'admin',
    'CONCOURSE_CLIENT_ID': None,
    'CONCOURSE_CLIENT_SECRET': None,
    'CONCOURSE_CLIENT_TOKEN_URL': None,
    'CONCOURSE_TEAM': 'main'
}


def create_app():
    app = Flask(__name__, static_url_path='', static_folder='public')

    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')

    # copy these environment variables into app.config
    for ck, default in CONFIG_KEYS.items():
        app.config[ck] = os.environ.get(ck, default)

    logging.info('Loaded application configuration:')
    for ck in sorted(CONFIG_KEYS.keys()):
        logging.info('{0}: {1}'.format(ck, app.config[ck]))

    # add route for the statics
    app.add_url_rule('/', 'root', lambda: app.send_static_file('index.html'))

    # cached bearer token
    @app.before_request
    def set_globals():
        if 'bearerToken' not in g:
            g.bearerToken = ''
        if 'idx' not in g:
            g.idx = 0

    @app.after_request
    def set_headers(response):
        response.cache_control.no_cache = True
        response.cache_control.no_store = True
        response.cache_control.must_revalidate = True
        response.cache_control.private = True
        response.headers['Pragma'] = 'no-cache'
        return response

    @app.route('/api/v1/pipelines', methods=['GET'])
    def redirectPipelines():
        '''
            Make requests to the concourseCI and collect easy-to-parse output
            about pipelines and job statuses

        '''

        # Get fresh auth header
        tokenHeader = _getAuthenticationHeader()

        # Get list of all the pipelines
        try:
            r = requests.get(app.config['CONCOURSE_URL'] + '/api/v1/pipelines', headers=tokenHeader)
            r.raise_for_status()
        except requests.ConnectionError as e:
            return Response("The ConcourseCI is not reachable", status=500, headers={'Etag': ''})
        except requests.exceptions.HTTPError as e:
            return Response("The ConcourseCI is not reachable, status code: " + str(e.response.status_code) +
                            ", reason: " + e.response.reason, status=500, headers={'Etag': ''})

        # Check that at least one worker is available
        try:
            responseWorkers = requests.get(app.config['CONCOURSE_URL'] + '/api/v1/workers', headers=tokenHeader)
            responseWorkers.raise_for_status()
            if len(responseWorkers.json()) == 0:
                return Response("There are no workers available!", status=500, headers={'Etag': ''})

        except requests.exceptions.HTTPError as e:
            return Response("The ConcourseCI is not reachable, status code: " + str(e.response.status_code) +
                            ", reason: " + e.response.reason, status=500, headers={'Etag': ''})

        # iterate over pipelines and find the status for each
        lstPipelines = []
        for pipeline in r.json():
            details = {}
            details['url'] = app.config['CONCOURSE_URL'] + pipeline['url']
            details['name'] = pipeline['name']
            details['paused'] = pipeline['paused']

            if (not pipeline["paused"]):
                lstJobs = []

                rr = requests.get(app.config['CONCOURSE_URL'] + '/api/v1/teams/' +
                                  app.config['CONCOURSE_TEAM'] + '/pipelines/' + pipeline['name'] +
                                  '/jobs', headers=tokenHeader)
                for job in rr.json():
                    if job['next_build']:
                        lstJobs.append({
                            'status': job['next_build']['status'],
                            'id': job['next_build']['id']
                        })
                    elif job['finished_build']:
                        lstJobs.append({
                            'status': job['finished_build']['status'],
                            'id': job['finished_build']['id']
                        })
                    else:
                        lstJobs.append({'status': 'non-exist'})

                details['jobs'] = lstJobs

            lstPipelines.append(details)

        # sort pipelines by name
        lstPipelines = sorted(lstPipelines, key=lambda pipeline: pipeline['name'])

        jsonResponse = json.dumps(lstPipelines)

        # SHA1 should generate well-behaved etags
        etag = hashlib.sha1(jsonResponse.encode('utf-8')).hexdigest()
        requestEtag = request.headers.get('If-None-Match', '')

        if requestEtag == etag:
            # the concourse status wasn't modify. Return only "not modified" status code, avoiding to refresh the page
            return Response(
                status=304,
                mimetype='application/json',
                headers={
                    'Cache-Control': 'public',
                    'Access-Control-Allow-Origin': '*',
                    'Etag': etag
                })

        else:
            # there were changes since the last call. Return the full response
            return Response(
                jsonResponse,
                mimetype='application/json',
                headers={
                    'Cache-Control': 'public',
                    'Access-Control-Allow-Origin': '*',
                    'Etag': etag
                })

    def _getAuthenticationHeader():
        '''
            Method that returns the cached header for an authentication
            and updates the bearer token periodically, because token
            can be expired.
        '''

        if (g.idx == 0 or g.idx > 5000):

            # get the Bearer Token for the given team avoiding to request it again and again
            try:
                headers = {}
                auth = None
                if (app.config['CONCOURSE_CLIENT_ID'] and
                        app.config['CONCOURSE_CLIENT_SECRET'] and
                        app.config['CONCOURSE_CLIENT_TOKEN_URL']):
                    headers = _get_oauth_client_token()
                else:
                    auth = requests.auth.HTTPBasicAuth(app.config['CONCOURSE_USERNAME'],
                                                       app.config['CONCOURSE_PASSWORD'])
                r = requests.get(app.config['CONCOURSE_URL'] + '/api/v1/teams/' +
                                 app.config['CONCOURSE_TEAM'] + '/auth/token', auth=auth, headers=headers)
                r.raise_for_status()

                # remember the new
                g.bearerToken = r.json()['value']
                g.idx = 1

            except requests.exceptions.HTTPError:
                g.idx = 0
                return {"Authorization": "Bearer nonsence"}

        g.idx += 1
        return {"Authorization": "Bearer " + g.bearerToken}

    def _get_oauth_client_token():

        oauthToken = 'nonsense'
        try:
            r = requests.post(
                app.config['CONCOURSE_CLIENT_TOKEN_URL'],
                params={
                    'grant_type': 'client_credentials',
                    'response_type': 'token'
                },
                auth=requests.auth.HTTPBasicAuth(app.config['CONCOURSE_CLIENT_ID'],
                                                 app.config['CONCOURSE_CLIENT_SECRET'])
            )
            r.raise_for_status()

            oauthToken = r.json()['access_token']

        except requests.exceptions.HTTPError:
            pass

        return {"Authorization": "Bearer " + oauthToken}

    return app


if __name__ == '__main__':

    port = int(os.environ.get('PORT', 3001))
    app = create_app()
    app.run(host='0.0.0.0', port=port, debug=False)
