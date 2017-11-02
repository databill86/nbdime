#!/usr/bin/env python
# -*- coding:utf-8 -*-

from __future__ import print_function

import json
import os

from notebook.utils import url_path_join
from tornado.web import HTTPError, escape, authenticated
import nbformat

import nbdime
from .nbdimeserver import (
    template_path,
    static_path,
    NbdimeHandler,
    MainDifftoolHandler,
    ApiDiffHandler,
    APIHandler,
)

from ..gitfiles import changed_notebooks, is_path_in_repo, InvalidGitRepositoryError, BadName
from ..utils import split_os_path, EXPLICIT_MISSING_FILE, read_notebook


class AuthMainDifftoolHandler(MainDifftoolHandler):
    @authenticated
    def get(self):
        return super(AuthMainDifftoolHandler, self).get()


class GitDifftoolHandler(NbdimeHandler):
    """Diff tool handler that also handles showing diff to git HEAD"""

    @authenticated
    def get(self):
        args = self.base_args()
        args['base'] = 'git:' + self.get_argument('base', '')
        args['remote'] = ''

        self.write(self.render_template(
            'difftool.html',
            config_data=args,
            mathjax_url=self.mathjax_url,
            mathjax_config=self.mathjax_config,
            ))


class ExtensionApiDiffHandler(ApiDiffHandler):
    """Diff API handler that also handles diff to git HEAD"""

    def _get_git_notebooks(self, base):
         # Ensure path/root_dir that can be sent to git:
        root_dir = getattr(self.contents_manager, 'root_dir', None)
        if not is_path_in_repo(root_dir):
            # We need to traverse down 'base' until we find a repo
            for part in split_os_path(os.path.dirname(base)):
                root_dir = os.path.join(root_dir, part)
                if is_path_in_repo(root_dir):
                    break
            else:
                raise HTTPError(422, 'Invalid notebook: %s' % base)
            base = os.path.relpath(base, root_dir)

        # Get the base/remote notebooks:
        try:
            for fbase, fremote in changed_notebooks('HEAD', None, base, root_dir):
                base_nb = read_notebook(fbase, on_null='minimal')
                remote_nb = read_notebook(fremote, on_null='minimal')
                break  # there should only ever be one set of files
            else:
                # The filename was either invalid or the file is unchanged
                # Assume unchanged, and let read_notebook handle error
                # reporting if invalid
                base_nb = self.read_notebook(os.path.join(root_dir, base))
                remote_nb = base_nb
        except (InvalidGitRepositoryError, BadName) as e:
            self.log.exception(e)
            raise HTTPError(422, 'Invalid notebook: %s' % base)
        return base_nb, remote_nb

    @authenticated
    def post(self):
        # Assuming a request on the form "{'argname':arg}"
        body = json.loads(escape.to_unicode(self.request.body))
        base = body['base']
        if base.startswith('git:'):
            base_nb, remote_nb = self._get_git_notebooks(base[len('git:'):])
        else:
            # Regular files, call super
            return super(ExtensionApiDiffHandler, self).post()

        # Perform actual diff and return data:
        try:
            thediff = nbdime.diff_notebooks(base_nb, remote_nb)
        except Exception:
            self.log.exception('Error diffing documents:')
            raise HTTPError(500, 'Error while attempting to diff documents')

        data = {
            'base': base_nb,
            'diff': thediff,
            }
        self.finish(data)

    @property
    def curdir(self):
        root_dir = getattr(self.contents_manager, 'root_dir', None)
        if root_dir is None:
            return super(ExtensionApiDiffHandler, self).curdir
        return root_dir


class IsGitHandler(NbdimeHandler, APIHandler):
    """API handler for querying if path is in git repo"""

    @authenticated
    def post(self):
        root_dir = getattr(self.contents_manager, 'root_dir', None)
        # Ensure notebooks are file-system based
        if root_dir is None:
            self.finish({'is_git': False})

        # Assuming a request on the form "{'argname':arg}"
        body = json.loads(escape.to_unicode(self.request.body))
        nb = os.path.join(root_dir, body['path'])

        data = {'is_git': is_path_in_repo(nb)}
        self.finish(data)


def _load_jupyter_server_extension(nb_server_app):
    """
    Called when the extension is loaded.

    Args:
        nb_server_app (NotebookWebApplication): handle to the Notebook webserver instance.
    """
    web_app = nb_server_app.web_app

    env = web_app.settings['jinja2_env']
    env.loader.searchpath.append(template_path)
    web_app.settings['template_path'].append(template_path)

    web_app.settings['static_path'].append(static_path)

    params = {
        'nbdime_relative_base_url': 'nbdime',
        'closable': False,
    }
    handlers = [
        (r'/nbdime/difftool', AuthMainDifftoolHandler, params),
        (r'/nbdime/git-difftool', GitDifftoolHandler, params),
        (r'/nbdime/api/diff', ExtensionApiDiffHandler, params),
        (r'/nbdime/api/isgit', IsGitHandler, params),
    ]

    # Prefix routes with base_url:
    base_url = web_app.settings.get('base_url', '/')
    handlers = [(url_path_join(base_url, h[0]), h[1], h[2]) for h in handlers]

    host_pattern = '.*$'
    web_app.add_handlers(host_pattern, handlers)
