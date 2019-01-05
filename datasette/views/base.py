import asyncio
import csv
import itertools
import json
import re
import time
import urllib

import jinja2
import pint
from sanic import response
from sanic.exceptions import NotFound
from sanic.views import HTTPMethodView

from datasette import __version__
from datasette.plugins import pm
from datasette.utils import (
    CustomJSONEncoder,
    InterruptedError,
    InvalidSql,
    LimitedWriter,
    is_url,
    path_from_row_pks,
    path_with_added_args,
    path_with_format,
    remove_infinites,
    resolve_table_and_format,
    sqlite3,
    to_css_class,
    value_as_boolean,
)

ureg = pint.UnitRegistry()

HASH_LENGTH = 7


class DatasetteError(Exception):

    def __init__(self, message, title=None, error_dict=None, status=500, template=None, messagge_is_html=False):
        self.message = message
        self.title = title
        self.error_dict = error_dict or {}
        self.status = status
        self.messagge_is_html = messagge_is_html


class RenderMixin(HTTPMethodView):

    def _asset_urls(self, key, template, context):
        # Flatten list-of-lists from plugins:
        seen_urls = set()
        for url_or_dict in itertools.chain(
            itertools.chain.from_iterable(getattr(pm.hook, key)(
                template=template.name,
                database=context.get("database"),
                table=context.get("table"),
                datasette=self.ds
            )),
            (self.ds.metadata(key) or [])
        ):
            if isinstance(url_or_dict, dict):
                url = url_or_dict["url"]
                sri = url_or_dict.get("sri")
            else:
                url = url_or_dict
                sri = None
            if url in seen_urls:
                continue
            seen_urls.add(url)
            if sri:
                yield {"url": url, "sri": sri}
            else:
                yield {"url": url}

    def render(self, templates, **context):
        template = self.ds.jinja_env.select_template(templates)
        select_templates = [
            "{}{}".format("*" if template_name == template.name else "", template_name)
            for template_name in templates
        ]
        body_scripts = []
        for script in pm.hook.extra_body_script(
            template=template.name,
            database=context.get("database"),
            table=context.get("table"),
            datasette=self.ds
        ):
            body_scripts.append(jinja2.Markup(script))
        return response.html(
            template.render(
                {
                    **context,
                    **{
                        "app_css_hash": self.ds.app_css_hash(),
                        "select_templates": select_templates,
                        "zip": zip,
                        "body_scripts": body_scripts,
                        "extra_css_urls": self._asset_urls(
                            "extra_css_urls", template, context
                        ),
                        "extra_js_urls": self._asset_urls(
                            "extra_js_urls", template, context
                        ),
                        "prefix": self.ds.config("url_prefix") or "/",
                    }
                }
            )
        )


class BaseView(RenderMixin):
    re_named_parameter = re.compile(":([a-zA-Z0-9_]+)")

    def __init__(self, datasette):
        self.ds = datasette

    def table_metadata(self, database, table):
        "Fetch table-specific metadata."
        return (self.ds.metadata("databases") or {}).get(database, {}).get(
            "tables", {}
        ).get(
            table, {}
        )

    def options(self, request, *args, **kwargs):
        r = response.text("ok")
        if self.ds.cors:
            r.headers["Access-Control-Allow-Origin"] = "*"
        return r

    def redirect(self, request, path, forward_querystring=True):
        if request.query_string and "?" not in path and forward_querystring:
            path = "{}?{}".format(path, request.query_string)
        r = response.redirect(path)
        r.headers["Link"] = "<{}>; rel=preload".format(path)
        if self.ds.cors:
            r.headers["Access-Control-Allow-Origin"] = "*"
        return r

    def resolve_db_name(self, db_name, **kwargs):
        databases = self.ds.inspect()
        hash = None
        name = None
        if "-" in db_name:
            # Might be name-and-hash, or might just be
            # a name with a hyphen in it
            name, hash = db_name.rsplit("-", 1)
            if name not in databases:
                # Try the whole name
                name = db_name
                hash = None
        else:
            name = db_name
        # Verify the hash
        try:
            info = databases[name]
        except KeyError:
            raise NotFound("Database not found: {}".format(name))

        expected = info["hash"][:HASH_LENGTH]
        if expected != hash:
            if "table_and_format" in kwargs:
                table, _format = resolve_table_and_format(
                    table_and_format=urllib.parse.unquote_plus(
                        kwargs["table_and_format"]
                    ),
                    table_exists=lambda t: self.ds.table_exists(name, t)
                )
                kwargs["table"] = table
                if _format:
                    kwargs["as_format"] = ".{}".format(_format)
            elif "table" in kwargs:
                kwargs["table"] = urllib.parse.unquote_plus(
                    kwargs["table"]
                )

            should_redirect = "/{}-{}".format(name, expected)
            if "table" in kwargs:
                should_redirect += "/" + urllib.parse.quote_plus(
                    kwargs["table"]
                )
            if "pk_path" in kwargs:
                should_redirect += "/" + kwargs["pk_path"]
            if "as_format" in kwargs:
                should_redirect += kwargs["as_format"]
            if "as_db" in kwargs:
                should_redirect += kwargs["as_db"]
            return name, expected, should_redirect

        return name, expected, None

    def absolute_url(self, request, path):
        url = urllib.parse.urljoin(self.ds.config("url_prefix") or request.url, path)
        if url.startswith("http://") and self.ds.config("force_https_urls"):
            url = "https://" + url[len("http://"):]
        return url

    def get_templates(self, database, table=None):
        assert NotImplemented

    async def get(self, request, db_name, **kwargs):
        database, hash, should_redirect = self.resolve_db_name(db_name, **kwargs)
        if should_redirect:
            return self.redirect(request, should_redirect)

        return await self.view_get(request, database, hash, **kwargs)

    async def as_csv(self, request, database, hash, **kwargs):
        stream = request.args.get("_stream")
        if stream:
            # Some quick sanity checks
            if not self.ds.config("allow_csv_stream"):
                raise DatasetteError("CSV streaming is disabled", status=400)
            if request.args.get("_next"):
                raise DatasetteError(
                    "_next not allowed for CSV streaming", status=400
                )
            kwargs["_size"] = "max"
        # Fetch the first page
        try:
            response_or_template_contexts = await self.data(
                request, database, hash, **kwargs
            )
            if isinstance(response_or_template_contexts, response.HTTPResponse):
                return response_or_template_contexts
            else:
                data, extra_template_data, templates = response_or_template_contexts
        except (sqlite3.OperationalError, InvalidSql) as e:
            raise DatasetteError(str(e), title="Invalid SQL", status=400)

        except (sqlite3.OperationalError) as e:
            raise DatasetteError(str(e))

        except DatasetteError:
            raise

        # Convert rows and columns to CSV
        headings = data["columns"]
        # if there are expanded_columns we need to add additional headings
        expanded_columns = set(data.get("expanded_columns") or [])
        if expanded_columns:
            headings = []
            for column in data["columns"]:
                headings.append(column)
                if column in expanded_columns:
                    headings.append("{}_label".format(column))

        async def stream_fn(r):
            nonlocal data
            writer = csv.writer(LimitedWriter(r, self.ds.config("max_csv_mb")))
            first = True
            next = None
            while first or (next and stream):
                try:
                    if next:
                        kwargs["_next"] = next
                    if not first:
                        data, extra_template_data, templates = await self.data(
                            request, database, hash, **kwargs
                        )
                    if first:
                        writer.writerow(headings)
                        first = False
                    next = data.get("next")
                    for row in data["rows"]:
                        if not expanded_columns:
                            # Simple path
                            writer.writerow(row)
                        else:
                            # Look for {"value": "label": } dicts and expand
                            new_row = []
                            for cell in row:
                                if isinstance(cell, dict):
                                    new_row.append(cell["value"])
                                    new_row.append(cell["label"])
                                else:
                                    new_row.append(cell)
                            writer.writerow(new_row)
                except Exception as e:
                    print('caught this', e)
                    r.write(str(e))
                    return

        content_type = "text/plain; charset=utf-8"
        headers = {}
        if self.ds.cors:
            headers["Access-Control-Allow-Origin"] = "*"
        if request.args.get("_dl", None):
            content_type = "text/csv; charset=utf-8"
            disposition = 'attachment; filename="{}.csv"'.format(
                kwargs.get('table', database)
            )
            headers["Content-Disposition"] = disposition

        return response.stream(
            stream_fn,
            headers=headers,
            content_type=content_type
        )

    async def view_get(self, request, database, hash, **kwargs):
        # If ?_format= is provided, use that as the format
        _format = request.args.get("_format", None)
        if not _format:
            _format = (kwargs.pop("as_format", None) or "").lstrip(".")
        if "table_and_format" in kwargs:
            table, _ext_format = resolve_table_and_format(
                table_and_format=urllib.parse.unquote_plus(
                    kwargs["table_and_format"]
                ),
                table_exists=lambda t: self.ds.table_exists(database, t)
            )
            _format = _format or _ext_format
            kwargs["table"] = table
            del kwargs["table_and_format"]
        elif "table" in kwargs:
            kwargs["table"] = urllib.parse.unquote_plus(
                kwargs["table"]
            )

        if _format == "csv":
            return await self.as_csv(request, database, hash, **kwargs)

        if _format is None:
            # HTML views default to expanding all forign key labels
            kwargs['default_labels'] = True

        extra_template_data = {}
        start = time.time()
        status_code = 200
        templates = []
        try:
            response_or_template_contexts = await self.data(
                request, database, hash, **kwargs
            )
            if isinstance(response_or_template_contexts, response.HTTPResponse):
                return response_or_template_contexts

            else:
                data, extra_template_data, templates = response_or_template_contexts
        except InterruptedError as e:
            raise DatasetteError("""
                SQL query took too long. The time limit is controlled by the
                <a href="https://datasette.readthedocs.io/en/stable/config.html#sql-time-limit-ms">sql_time_limit_ms</a>
                configuration option.
            """, title="SQL Interrupted", status=400, messagge_is_html=True)
        except (sqlite3.OperationalError, InvalidSql) as e:
            raise DatasetteError(str(e), title="Invalid SQL", status=400)

        except (sqlite3.OperationalError) as e:
            raise DatasetteError(str(e))

        except DatasetteError:
            raise

        end = time.time()
        data["query_ms"] = (end - start) * 1000
        for key in ("source", "source_url", "license", "license_url"):
            value = self.ds.metadata(key)
            if value:
                data[key] = value
        if _format in ("json", "jsono"):
            # Special case for .jsono extension - redirect to _shape=objects
            if _format == "jsono":
                return self.redirect(
                    request,
                    path_with_added_args(
                        request,
                        {"_shape": "objects"},
                        path=request.path.rsplit(".jsono", 1)[0] + ".json",
                    ),
                    forward_querystring=False,
                )

            # Handle the _json= parameter which may modify data["rows"]
            json_cols = []
            if "_json" in request.args:
                json_cols = request.args["_json"]
            if json_cols and "rows" in data and "columns" in data:
                data["rows"] = convert_specific_columns_to_json(
                    data["rows"], data["columns"], json_cols,
                )

            # unless _json_infinity=1 requested, replace infinity with None
            if "rows" in data and not value_as_boolean(
                request.args.get("_json_infinity", "0")
            ):
                data["rows"] = [remove_infinites(row) for row in data["rows"]]

            # Deal with the _shape option
            shape = request.args.get("_shape", "arrays")
            if shape == "arrayfirst":
                data = [row[0] for row in data["rows"]]
            elif shape in ("objects", "object", "array"):
                columns = data.get("columns")
                rows = data.get("rows")
                if rows and columns:
                    data["rows"] = [dict(zip(columns, row)) for row in rows]
                if shape == "object":
                    error = None
                    if "primary_keys" not in data:
                        error = "_shape=object is only available on tables"
                    else:
                        pks = data["primary_keys"]
                        if not pks:
                            error = "_shape=object not available for tables with no primary keys"
                        else:
                            object_rows = {}
                            for row in data["rows"]:
                                pk_string = path_from_row_pks(row, pks, not pks)
                                object_rows[pk_string] = row
                            data = object_rows
                    if error:
                        data = {
                            "ok": False,
                            "error": error,
                            "database": database,
                            "database_hash": hash,
                        }
                elif shape == "array":
                    data = data["rows"]
            elif shape == "arrays":
                pass
            else:
                status_code = 400
                data = {
                    "ok": False,
                    "error": "Invalid _shape: {}".format(shape),
                    "status": 400,
                    "title": None,
                }
            headers = {}
            if self.ds.cors:
                headers["Access-Control-Allow-Origin"] = "*"
            r = response.HTTPResponse(
                json.dumps(data, cls=CustomJSONEncoder),
                status=status_code,
                content_type="application/json",
                headers=headers,
            )
        else:
            extras = {}
            if callable(extra_template_data):
                extras = extra_template_data()
                if asyncio.iscoroutine(extras):
                    extras = await extras
            else:
                extras = extra_template_data
            url_labels_extra = {}
            if data.get("expandable_columns"):
                url_labels_extra = {"_labels": "on"}
            url_csv_args = {
                "_size": "max",
                **url_labels_extra
            }
            url_csv = path_with_format(request, "csv", url_csv_args)
            url_csv_path = url_csv.split('?')[0]
            context = {
                **data,
                **extras,
                **{
                    "url_json": path_with_format(request, "json", {
                        **url_labels_extra,
                    }),
                    "url_csv": url_csv,
                    "url_csv_path": url_csv_path,
                    "url_csv_hidden_args": [
                        (key, value)
                        for key, value in urllib.parse.parse_qsl(request.query_string)
                        if key not in ("_labels", "_facet", "_size")
                    ] + [("_size", "max")],
                    "datasette_version": __version__,
                    "config": self.ds.config_dict(),
                }
            }
            if "metadata" not in context:
                context["metadata"] = self.ds.metadata
            r = self.render(templates, **context)
            r.status = status_code
        # Set far-future cache expiry
        if self.ds.cache_headers:
            ttl = request.args.get("_ttl", None)
            if ttl is None or not ttl.isdigit():
                ttl = self.ds.config("default_cache_ttl")
            else:
                ttl = int(ttl)
            if ttl == 0:
                ttl_header = 'no-cache'
            else:
                ttl_header = 'max-age={}'.format(ttl)
            r.headers["Cache-Control"] = ttl_header
        r.headers["Referrer-Policy"] = "no-referrer"
        return r

    async def custom_sql(
        self, request, database, hash, sql, editable=True, canned_query=None,
        metadata=None, _size=None
    ):
        params = request.raw_args
        if "sql" in params:
            params.pop("sql")
        if "_shape" in params:
            params.pop("_shape")
        # Extract any :named parameters
        named_parameters = self.re_named_parameter.findall(sql)
        named_parameter_values = {
            named_parameter: params.get(named_parameter) or ""
            for named_parameter in named_parameters
        }

        # Set to blank string if missing from params
        for named_parameter in named_parameters:
            if named_parameter not in params:
                params[named_parameter] = ""

        extra_args = {}
        if params.get("_timelimit"):
            extra_args["custom_time_limit"] = int(params["_timelimit"])
        if _size:
            extra_args["page_size"] = _size
        results = await self.ds.execute(
            database, sql, params, truncate=True, **extra_args
        )
        columns = [r[0] for r in results.description]

        templates = ["query-{}.html".format(to_css_class(database)), "query.html"]
        if canned_query:
            templates.insert(
                0,
                "query-{}-{}.html".format(
                    to_css_class(database), to_css_class(canned_query)
                ),
            )

        async def extra_template():
            display_rows = []
            for row in results.rows:
                display_row = []
                for column, value in zip(results.columns, row):
                    display_value = value
                    # Let the plugins have a go
                    plugin_value = pm.hook.render_cell(
                        value=value,
                        column=column,
                        table=None,
                        database=database,
                        datasette=self.ds,
                    )
                    if plugin_value is not None:
                        display_value = plugin_value
                    else:
                        if value in ("", None):
                            display_value = jinja2.Markup("&nbsp;")
                        elif is_url(str(display_value).strip()):
                            display_value = jinja2.Markup(
                                '<a href="{url}">{url}</a>'.format(
                                    url=jinja2.escape(value.strip())
                                )
                            )
                    display_row.append(display_value)
                display_rows.append(display_row)
            return {
                "display_rows": display_rows,
                "database_hash": hash,
                "custom_sql": True,
                "named_parameter_values": named_parameter_values,
                "editable": editable,
                "canned_query": canned_query,
                "metadata": metadata,
                "config": self.ds.config_dict(),
            }

        return {
            "database": database,
            "rows": results.rows,
            "truncated": results.truncated,
            "columns": columns,
            "query": {"sql": sql, "params": params},
        }, extra_template, templates


def convert_specific_columns_to_json(rows, columns, json_cols):
    json_cols = set(json_cols)
    if not json_cols.intersection(columns):
        return rows
    new_rows = []
    for row in rows:
        new_row = []
        for value, column in zip(row, columns):
            if column in json_cols:
                try:
                    value = json.loads(value)
                except (TypeError, ValueError) as e:
                    print(e)
                    pass
            new_row.append(value)
        new_rows.append(new_row)
    return new_rows
