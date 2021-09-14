from flask import Flask
import logging
from pythonjsonlogger.jsonlogger import JsonFormatter
from werkzeug.middleware.proxy_fix import ProxyFix

from hydrant.views import base_blueprint
from hydrant.logserverhandler import LogServerHandler


def create_app(testing=False, cli=False):
    """Application factory, used to create application
    """
    app = Flask('hydrant')
    app.config.from_object('hydrant.config')
    app.config['TESTING'] = testing

    register_blueprints(app)
    configure_logging(app)
    configure_proxy(app)

    return app


def register_blueprints(app):
    """register all blueprints for application
    """
    app.register_blueprint(base_blueprint)


def configure_logging(app):
    app.logger.setLevel(getattr(logging, app.config['LOG_LEVEL'].upper()))

    if not app.config['LOGSERVER_URL']:
        return

    log_server_handler = LogServerHandler(
        level=logging.INFO,
        jwt=app.config['LOGSERVER_TOKEN'],
        url=app.config['LOGSERVER_URL'])

    json_formatter = JsonFormatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s")
    log_server_handler.setFormatter(json_formatter)

    event_logger = logging.getLogger("event_logger")
    event_logger.setLevel(logging.INFO)
    event_logger.addHandler(log_server_handler)
    app.logger.debug("hydrant <app> logging initialized", extra={'tags': ['testing', 'logging']})
    event_logger.info("hydrant <event> logging initialized", extra={'tags': ['testing', 'logging']})


def configure_proxy(app):
    """Add werkzeug fixer to detect headers applied by upstream reverse proxy"""
    if app.config.get('PREFERRED_URL_SCHEME', '').lower() == 'https':
        app.wsgi_app = ProxyFix(
            app=app.wsgi_app,

            # trust X-Forwarded-Host
            x_host=1,

            # trust X-Forwarded-Port
            x_port=1,
        )

