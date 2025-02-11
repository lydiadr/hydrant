import click
from flask import Blueprint, abort, current_app, jsonify
from flask.json import JSONEncoder
import jmespath
import importlib
import requests
import sys

from hydrant.audit import audit_entry
from hydrant.models.bundle import Bundle

base_blueprint = Blueprint('base', __name__, cli_group=None)


@base_blueprint.route('/')
def root():
    return {"message": "ok"}


@base_blueprint.route('/settings', defaults={'config_key': None})
@base_blueprint.route('/settings/<string:config_key>')
def config_settings(config_key):
    """Non-secret application settings"""

    # workaround no JSON representation for datetime.timedelta
    class CustomJSONEncoder(JSONEncoder):
        def default(self, obj):
            return str(obj)
    current_app.json_encoder = CustomJSONEncoder

    # return selective keys - not all can be be viewed by users, e.g.secret key
    blacklist = ('SECRET', 'KEY')

    if config_key:
        key = config_key.upper()
        for pattern in blacklist:
            if pattern in key:
                abort(status_code=400, messag=f"Configuration key {key} not available")
        return jsonify({key: current_app.config.get(key)})

    config_settings = {}
    for key in current_app.config:
        matches = any(pattern for pattern in blacklist if pattern in key)
        if matches:
            continue
        config_settings[key] = current_app.config.get(key)

    return jsonify(config_settings)


@base_blueprint.cli.command("export")
@click.argument("adapter")
@click.option("--filter", help="HAPI FHIR filter parameters")
def export(adapter, filter):
    """Export data using the named adapter

    Named adapter knows how to connect to data source and format for export
    """
    from hydrant.adapters.csv import CSV_Serializer

    # attempt to load adapter class from configured modules
    m1 = importlib.import_module('hydrant.adapters.sites.kent')
    m2 = importlib.import_module('hydrant.adapters.sites.skagit')
    adapter_class = None
    for module in m1, m2:
        if hasattr(module, adapter):
            adapter_class = getattr(module, adapter)

    if not adapter_class:
        raise click.BadParameter(f"Adapter class not found: {adapter}")

    # Pull resources from backing store and generate export via adapter class
    target_system = current_app.config['FHIR_SERVER_URL']
    search_url = '/'.join((target_system, adapter_class.RESOURCE_CLASS.RESOURCE_TYPE))
    if filter:
        search_url = '?'.join((search_url, filter))
    response = requests.get(search_url)
    bundle = response.json()
    assert bundle['resourceType'] == 'Bundle'
    serializer = CSV_Serializer(sys.stdout)
    serializer.headers(adapter_class.headers())

    # Bundle will potentially include pages of results
    total = 0
    while True:
        if 'entry' not in bundle:
            break

        for entry in bundle['entry']:
            item = adapter_class(parsed_row=None)
            serializer.add_row(item.from_resource(entry['resource']))
            total += 1
        serializer.flush()
        next_page_link = jmespath.search('link[?relation==`next`].{url: url}', bundle)
        if not next_page_link:
            break
        response = requests.get(next_page_link[0]['url'])
        bundle = response.json()

    # Write to stderr so as to not pollute output file
    click.echo(f"Exported {total} {adapter_class.RESOURCE_CLASS.RESOURCE_TYPE}s", err=True)


@base_blueprint.cli.command("upload")
@click.argument("filename")
def upload_file(filename):
    """Parse and upload content in named file

    Seek out given filename from configured upload directory.  Parse
    the file, and push results to configured FHIR store.
    """
    try:
        with open(filename, 'r') as f:
            pass
    except FileNotFoundError:
        raise click.FileError(f"'{filename}'", "File not found")

    # Locate best parser and adapter
    # TODO: move this process to factory methods
    parser, adapter = None, None
    if filename.endswith('csv'):
        from hydrant.adapters.csv import CSV_Parser
        from hydrant.adapters.sites.kent import KentPatientAdapter
        from hydrant.adapters.sites.skagit import SkagitPatientAdapter, SkagitServiceRequestAdapter
        from hydrant.models.resource_list import ResourceList

        parser = CSV_Parser(filename)
        headers = set(parser.headers)

        # sniff out the site adapter from the header values
        for site_adapter in (KentPatientAdapter, SkagitPatientAdapter, SkagitServiceRequestAdapter):
            if not set(site_adapter.headers()).difference(headers):
                if adapter:
                    raise click.BadParameter("column headers match multiple adapters")
                adapter = site_adapter
        if not adapter:
            raise click.BadParameter("column headers not found in any available adapters")
    else:
        raise click.BadParameter("no appropriate parsers found; can't continue")

    # With parser and adapter at hand, process the data
    target_system = current_app.config['FHIR_SERVER_URL']
    bundle = Bundle()
    resources = ResourceList(parser, adapter)

    for r in resources:
        bundle.add_entry(r.as_upsert_entry())

    fhir_bundle = bundle.as_fhir()
    click.echo(f"  - parsed {fhir_bundle['total']}")
    click.echo(f"  - uploading bundle to {target_system}")
    extra = {'tags': [adapter.RESOURCE_CLASS.RESOURCE_TYPE, 'upload'], 'user': 'system'}
    current_app.logger.info(
        f"upload {fhir_bundle['total']} from {filename}",
        extra=extra)

    response = requests.post(target_system, json=fhir_bundle)
    click.echo(f"  - response status {response.status_code}")
    audit_entry(f"uploaded: {response.json()}", extra=extra)

    if response.status_code != 200:
        raise click.BadParameter(response.text)

    click.echo("upload complete")
