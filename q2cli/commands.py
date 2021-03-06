# ----------------------------------------------------------------------------
# Copyright (c) 2016-2019, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import collections

import click

import q2cli.dev
import q2cli.info
import q2cli.tools


class RootCommand(click.MultiCommand):
    """This class defers to either the PluginCommand or the builtin cmds"""
    _builtin_commands = collections.OrderedDict([
        ('info', q2cli.info.info),
        ('tools', q2cli.tools.tools),
        ('dev', q2cli.dev.dev)
    ])

    def __init__(self, *args, **kwargs):
        import re
        import sys

        unicodes = ["\u2018", "\u2019", "\u201C", "\u201D", "\u2014"]
        category_regex = re.compile(r'--m-(\S+)-category')

        invalid_chars = []
        categories = []
        for command in sys.argv:
            if any(x in command for x in unicodes):
                invalid_chars.append(command)

            match = category_regex.fullmatch(command)
            if match is not None:
                param_name, = match.groups()
                # Maps old-style option name to new name.
                categories.append((command, '--m-%s-column' % param_name))

        if invalid_chars or categories:
            if invalid_chars:
                click.secho("Error: Detected invalid character in: %s\n"
                            "Verify the correct quotes or dashes (ASCII) are "
                            "being used." % ', '.join(invalid_chars),
                            err=True, fg='red', bold=True)
            if categories:
                old_to_new_names = '\n'.join(
                    'Instead of %s, trying using %s' % (old, new)
                    for old, new in categories)
                msg = ("Error: The following options no longer exist because "
                       "metadata *categories* are now called metadata "
                       "*columns* in QIIME 2.\n\n%s" % old_to_new_names)
                click.secho(msg, err=True, fg='red', bold=True)
            sys.exit(-1)

        super().__init__(*args, **kwargs)

        # Plugin state for current deployment that will be loaded from cache.
        # Used to construct the dynamic CLI.
        self._plugins = None

    @property
    def _plugin_lookup(self):
        import q2cli.util

        # See note in `q2cli.completion.write_bash_completion_script` for why
        # `self._plugins` will not always be obtained from
        # `q2cli.cache.CACHE.plugins`.
        if self._plugins is None:
            import q2cli.cache
            self._plugins = q2cli.cache.CACHE.plugins

        name_map = {}
        for name, plugin in self._plugins.items():
            if plugin['actions']:
                name_map[q2cli.util.to_cli_name(name)] = plugin
        return name_map

    def list_commands(self, ctx):
        import itertools

        # Avoid sorting builtin commands as they have a predefined order based
        # on applicability to users. For example, it isn't desirable to have
        # the `dev` command listed before `info` and `tools`.
        builtins = self._builtin_commands
        plugins = sorted(self._plugin_lookup)
        return itertools.chain(builtins, plugins)

    def get_command(self, ctx, name):
        if name in self._builtin_commands:
            return self._builtin_commands[name]

        try:
            plugin = self._plugin_lookup[name]
        except KeyError:
            return None

        return PluginCommand(plugin, name)


class PluginCommand(click.MultiCommand):
    """Provides ActionCommands based on available Actions"""
    def __init__(self, plugin, name, *args, **kwargs):
        import q2cli.util

        # the cli currently doesn't differentiate between methods
        # and visualizers, it treats them generically as Actions
        self._plugin = plugin
        self._action_lookup = {q2cli.util.to_cli_name(id): a for id, a in
                               plugin['actions'].items()}

        support = 'Getting user support: %s' % plugin['user_support_text']
        website = 'Plugin website: %s' % plugin['website']
        description = 'Description: %s' % plugin['description']
        help_ = '\n\n'.join([description, website, support])

        params = [
            click.Option(('--version',), is_flag=True, expose_value=False,
                         is_eager=True, callback=self._get_version,
                         help='Show the version and exit.'),
            q2cli.util.citations_option(self._get_citation_records)
        ]

        super().__init__(name, *args, short_help=plugin['short_description'],
                         help=help_, params=params, **kwargs)

    def _get_version(self, ctx, param, value):
        if not value or ctx.resilient_parsing:
            return

        click.echo('%s version %s' % (self._plugin['name'],
                                      self._plugin['version']))
        ctx.exit()

    def _get_citation_records(self):
        import qiime2.sdk
        pm = qiime2.sdk.PluginManager()
        return pm.plugins[self._plugin['name']].citations

    def list_commands(self, ctx):
        return sorted(self._action_lookup)

    def get_command(self, ctx, name):
        try:
            action = self._action_lookup[name]
        except KeyError:
            click.echo("Error: QIIME 2 plugin %r has no action %r."
                       % (self._plugin['name'], name), err=True)
            ctx.exit(2)  # Match exit code of `return None`

        return ActionCommand(name, self._plugin, action)


class ActionCommand(click.Command):
    """A click manifestation of a QIIME 2 API Action (Method/Visualizer)

    The ActionCommand generates Handlers which map from 1 Action API parameter
    to one or more Click.Options.

    MetaHandlers are handlers which are not mapped to an API parameter, they
    are handled explicitly and generally return a `fallback` function which
    can be used to supplement value lookup in the regular handlers.
    """
    def __init__(self, name, plugin, action):
        import q2cli.handlers
        import q2cli.util

        self.plugin = plugin
        self.action = action
        self.generated_handlers = self.build_generated_handlers()
        self.verbose_handler = q2cli.handlers.VerboseHandler()
        self.quiet_handler = q2cli.handlers.QuietHandler()
        # Meta-Handlers:
        self.output_dir_handler = q2cli.handlers.OutputDirHandler()
        self.cmd_config_handler = q2cli.handlers.CommandConfigHandler(
            q2cli.util.to_cli_name(plugin['name']),
            q2cli.util.to_cli_name(self.action['id'])
        )
        super().__init__(name, params=list(self.get_click_parameters()),
                         callback=self, short_help=action['name'],
                         help=action['description'])

    def build_generated_handlers(self):
        import q2cli.handlers

        handler_map = {
            'input': q2cli.handlers.ArtifactHandler,
            'parameter': q2cli.handlers.parameter_handler_factory,
            'output': q2cli.handlers.ResultHandler
        }

        handlers = collections.OrderedDict()
        for item in self.action['signature']:
            item = item.copy()
            type = item.pop('type')

            if item['ast']['type'] == 'collection':
                inner_handler = handler_map[type](**item)
                handler = q2cli.handlers.CollectionHandler(inner_handler,
                                                           **item)
            else:
                handler = handler_map[type](**item)

            handlers[item['name']] = handler

        return handlers

    def get_click_parameters(self):
        import q2cli.util

        # Handlers may provide more than one click.Option
        for handler in self.generated_handlers.values():
            yield from handler.get_click_options()

        # Meta-Handlers' Options:
        yield from self.output_dir_handler.get_click_options()
        yield from self.cmd_config_handler.get_click_options()

        yield from self.verbose_handler.get_click_options()
        yield from self.quiet_handler.get_click_options()

        yield q2cli.util.citations_option(self._get_citation_records)

    def _get_citation_records(self):
        return self._get_action().citations

    def _get_action(self):
        import qiime2.sdk
        pm = qiime2.sdk.PluginManager()
        plugin = pm.plugins[self.plugin['name']]
        return plugin.actions[self.action['id']]

    def __call__(self, **kwargs):
        """Called when user hits return, **kwargs are Dict[click_names, Obj]"""
        import itertools
        import os
        import qiime2.util

        arguments, missing_in, verbose, quiet = self.handle_in_params(kwargs)
        outputs, missing_out = self.handle_out_params(kwargs)

        if missing_in or missing_out:
            # A new context is generated for a callback, which will result in
            # the ctx.command_path duplicating the action, so just use the
            # parent so we can print the help *within* a callback.
            ctx = click.get_current_context().parent
            click.echo(ctx.get_help()+"\n", err=True)
            for option in itertools.chain(missing_in, missing_out):
                click.secho("Error: Missing option: --%s" % option, err=True,
                            fg='red', bold=True)
            if missing_out:
                click.echo(_OUTPUT_OPTION_ERR_MSG, err=True)
            ctx.exit(1)

        action = self._get_action()
        # `qiime2.util.redirected_stdio` defaults to stdout/stderr when
        # supplied `None`.
        log = None

        if not verbose:
            import tempfile
            log = tempfile.NamedTemporaryFile(prefix='qiime2-q2cli-err-',
                                              suffix='.log',
                                              delete=False, mode='w')

        cleanup_logfile = False
        try:
            with qiime2.util.redirected_stdio(stdout=log, stderr=log):
                results = action(**arguments)
        except Exception as e:
            header = ('Plugin error from %s:'
                      % q2cli.util.to_cli_name(self.plugin['name']))
            if verbose:
                # log is not a file
                log = 'stderr'
            q2cli.util.exit_with_error(e, header=header, traceback=log)
        else:
            cleanup_logfile = True
        finally:
            # OS X will reap temporary files that haven't been touched in
            # 36 hours, double check that the log is still on the filesystem
            # before trying to delete. Otherwise this will fail and the
            # output won't be written.
            if log and cleanup_logfile and os.path.exists(log.name):
                log.close()
                os.remove(log.name)

        for result, output in zip(results, outputs):
            path = result.save(output)
            if not quiet:
                click.secho('Saved %s to: %s' % (result.type, path),
                            fg='green')

    def handle_in_params(self, kwargs):
        import q2cli.handlers

        arguments = {}
        missing = []
        cmd_fallback = self.cmd_config_handler.get_value(kwargs)

        verbose = self.verbose_handler.get_value(kwargs, fallback=cmd_fallback)
        quiet = self.quiet_handler.get_value(kwargs, fallback=cmd_fallback)

        if verbose and quiet:
            click.secho('Unsure of how to be quiet and verbose at the '
                        'same time.', fg='red', bold=True, err=True)
            click.get_current_context().exit(1)

        for item in self.action['signature']:
            if item['type'] == 'input' or item['type'] == 'parameter':
                name = item['name']
                handler = self.generated_handlers[name]
                try:
                    if isinstance(handler,
                                  (q2cli.handlers.MetadataHandler,
                                   q2cli.handlers.MetadataColumnHandler)):
                        arguments[name] = handler.get_value(
                            verbose, kwargs, fallback=cmd_fallback)
                    else:
                        arguments[name] = handler.get_value(
                            kwargs, fallback=cmd_fallback)
                except q2cli.handlers.ValueNotFoundException:
                    missing += handler.missing

        return arguments, missing, verbose, quiet

    def handle_out_params(self, kwargs):
        import q2cli.handlers

        outputs = []
        missing = []
        cmd_fallback = self.cmd_config_handler.get_value(kwargs)
        out_fallback = self.output_dir_handler.get_value(
            kwargs, fallback=cmd_fallback
        )

        def fallback(*args):
            try:
                return cmd_fallback(*args)
            except q2cli.handlers.ValueNotFoundException:
                return out_fallback(*args)

        for item in self.action['signature']:
            if item['type'] == 'output':
                name = item['name']
                handler = self.generated_handlers[name]

                try:
                    outputs.append(handler.get_value(kwargs,
                                                     fallback=fallback))
                except q2cli.handlers.ValueNotFoundException:
                    missing += handler.missing

        return outputs, missing


_OUTPUT_OPTION_ERR_MSG = """\
Note: When only providing names for a subset of the output Artifacts or
Visualizations, you must specify an output directory through use of the
--output-dir DIRECTORY flag.\
"""
