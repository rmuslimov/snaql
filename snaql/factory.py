# coding: utf-8
import os
import re
import copy
import collections
import types
from collections import namedtuple

from jinja2 import nodes
from jinja2.ext import Extension
from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2.loaders import split_template_path
from jinja2.utils import open_if_exists

from snaql.convertors import (
    escape_string,
    guard_string,
    guard_integer,
    guard_date,
    guard_datetime,
    guard_float,
    guard_timedelta,
    guard_time,
    guard_case,
)


class RawFileSystemLoader(FileSystemLoader):

    def get_source(self, environment, template):
        pieces = split_template_path(template)
        for searchpath in self.searchpath:
            filename = os.path.join(searchpath, *pieces)
            f = open_if_exists(filename)
            if f is None:
                continue
            try:
                contents = f.read().decode(self.encoding)
            finally:
                f.close()

            mtime = os.path.getmtime(filename)
            # Need to save original raw template before compilation
            environment.sql_params.setdefault('raws', {}).update({
                template: [c.strip() for c in contents.splitlines()]
            })

            def uptodate():
                try:
                    return os.path.getmtime(filename) == mtime
                except OSError:
                    return False
            return contents, filename, uptodate

        raise TemplateNotFound(template)


class JinjaSQLExtension(Extension):
    tags = set(['sql'])

    def parse(self, parser):
        lineno = next(parser.stream).lineno
        expr = parser.parse_expression()
        args = [expr]
        kwargs = [nodes.Keyword('func', expr)]
        if parser.stream.skip_if('comma'):
            # Optional 'note' for function docstring
            if (
                parser.stream.current.type == 'name'
                and parser.stream.current.value in ('note', 'cond_for')
            ):
                stream_type = parser.stream.current.value
                next(parser.stream)
                parser.stream.expect('assign')
                c_expr = parser.parse_expression()
                args.append(c_expr)
                kwargs.append(nodes.Keyword(stream_type, c_expr))

        body = parser.parse_statements(['name:endsql'], drop_needle=True)
        raw_template = self.environment.sql_params['raws'][parser.name]
        # Lines range of original raw template
        raw_lines = slice(lineno, parser.stream.current.lineno-1)
        self.environment.sql_params.setdefault('funcs', {}).update({
            expr.value: {'raw_sql': ' '.join(raw_template[raw_lines])}
        })
        call_node = nodes.Call(
            self.attr('_sql_process', lineno=lineno),
            args, kwargs, None, None
        )
        return nodes.CallBlock(call_node, [], [], body)

    def _sql_process(self, *args, **kwargs):
        caller = kwargs['caller']
        raw_sql = ' '.join(caller().split())
        if 'cond_for' in kwargs:
            origin = self.environment.sql_params['funcs'].get(kwargs['cond_for'])
            if origin:
                origin.setdefault('conds', []).append(kwargs['cond_for'])

        origin = self.environment.sql_params['funcs'].get(kwargs['func'])
        origin.update({
            'sql': raw_sql,
            'note': kwargs.get('note'),
            'is_cond': 'cond_for' in kwargs,
        })
        if origin['is_cond']:
            origin['cond_for'] = kwargs['cond_for']

        return raw_sql


class SnaqlException(Exception):
    pass


class Snaql(object):

    def __init__(self, sql_root, sql_ns):
        self.sql_root = sql_root
        self.jinja_env = Environment(
            trim_blocks=True,
            extensions=[JinjaSQLExtension],
            loader=RawFileSystemLoader(os.path.join(self.sql_root, sql_ns)),
        )
        self.jinja_env.filters.update({
            'guards.string': guard_string,
            'guards.integer': guard_integer,
            'guards.datetime': guard_datetime,
            'guards.date': guard_date,
            'guards.float': guard_float,
            'guards.timedelta': guard_timedelta,
            'guards.time': guard_time,
            'guards.case': guard_case,
        })
        self.jinja_env.extend(sql_params={})

    def gen_func(self, name, meta_struct, env):

        def subrender_cond(owner_name, cond_func, context):
            if isinstance(cond_func, collections.Callable) and cond_func.is_cond:
                if meta_struct['funcs'][cond_func.__name__]['cond_for'] != owner_name:
                    raise SnaqlException(
                        '"%s" is not proper condition for "%s"' % (
                            cond_func.__name__,
                            owner_name
                        )
                    )
                cond_tmpl = env.from_string(
                    meta_struct['funcs'][cond_func.__name__]['raw_sql']
                )
                return cond_tmpl.render(**context).strip()
            return cond_func

        def fn(**kwargs):
            if meta_struct['funcs'][name]['is_cond']:
                raise SnaqlException((
                    '"%s" is condition for "%s" and can not '
                    'be rendered outside of it\'s scope'
                ) % (name, meta_struct['funcs'][name]['cond_for']))
            if kwargs:
                for point, val in kwargs.items():
                    maybe_cond_sql = subrender_cond(name, val, kwargs)
                    if maybe_cond_sql:
                        kwargs[point] = maybe_cond_sql
                    if (
                        isinstance(val, collections.Iterable)
                        and not isinstance(val, types.StringTypes)
                    ):
                        val = [subrender_cond(name, v, kwargs) for v in val]
                        kwargs[point] = [v for v in val if v]

                sql_tmpl = env.from_string(meta_struct['funcs'][name]['raw_sql'])
                sql_raw = sql_tmpl.render(**kwargs).strip()
                return escape_string(sql_raw)

            return meta_struct['funcs'][name]['sql']

        fn.__doc__ = meta_struct['funcs'][name]['note']
        fn.is_cond = meta_struct['funcs'][name]['is_cond']
        fn.func_name = name

        return fn

    def load_queries(self, sql_path):
        template = self.jinja_env.get_template(sql_path)
        template.render()

        factory_methods = {}
        meta_struct = copy.deepcopy(self.jinja_env.sql_params)
        for name, block in self.jinja_env.sql_params['funcs'].items():
            fn = self.gen_func(name, meta_struct, self.jinja_env)
            factory_methods[name] = fn

        factory = namedtuple('SQLFactory', factory_methods.keys())
        struct = factory(*factory_methods.values())
        self.jinja_env.sql_params.clear()

        return struct
