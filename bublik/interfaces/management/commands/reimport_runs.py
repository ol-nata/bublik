# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 OKTET Labs Ltd. All rights reserved.

"""
Management command: reimport_runs
Usage: python manage.py reimport_runs [-i <id> ...] [-f <date>] [-t <date>]
       [-c <criterion> ...] [--done-after <datetime>] [--max-running <n>]
       [--hours <start>-<end>] [--poll <seconds>] [--task-timeout <hours>]
       [--dry-run]

Imports runs again with force from their logs, gradually, so that other imports
are not held up: no more than --max-running imports of the runs run at a time,
and the next one is scheduled only when no other task waits in the Celery queue.
Each run is imported into the project it belongs to, whatever project its
meta_data.json names.

The runs are selected by IDs and dates, and optionally by criteria (-c), which
all have to be met. Unfinished runs are never selected. With --done-after, runs
imported successfully since then are skipped, so that the command can be
stopped and run again to go on.

The command runs until all the imports are finished, which may take days, so it
is to be run in a terminal multiplexer such as tmux.
"""

from __future__ import annotations

from argparse import ArgumentTypeError
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils.dateparse import parse_datetime

from bublik.core.argparse import parser_type_date
from bublik.core.importruns.reimport import RUN_CRITERIA, GradualReimport, imported_after
from bublik.data.models import TestIterationResult


HOURS_IN_DAY = 24


def parser_type_datetime(s):
    moment = parse_datetime(s)
    if moment is None:
        msg = f'Invalid date and time format: {s}'
        raise ArgumentTypeError(msg)
    # Without an offset, the local time of the host
    return moment if moment.tzinfo else moment.astimezone()


def parser_type_hours(s):
    try:
        start, end = (int(hour) for hour in s.split('-'))
    except ValueError:
        msg = f'Invalid hours format: {s}'
        raise ArgumentTypeError(msg) from None
    if not (0 <= start < HOURS_IN_DAY and 0 <= end < HOURS_IN_DAY) or start == end:
        msg = f'Invalid hours: {s}'
        raise ArgumentTypeError(msg)
    return start, end


class Command(BaseCommand):
    help = 'Import runs again with force, gradually, so that other imports are not held up'

    def add_arguments(self, parser):
        parser.add_argument(
            '-i',
            '--id',
            type=int,
            action='append',
            default=[],
            metavar='run_id',
            help='Run ID to import again (repeatable: -i 1 -i 2).',
        )
        parser.add_argument(
            '-f',
            '--from',
            type=parser_type_date,
            help='Select runs with start date >= this date (YYYY.MM.DD).',
        )
        parser.add_argument(
            '-t',
            '--to',
            type=parser_type_date,
            help='Select runs with finish date <= this date (YYYY.MM.DD).',
        )
        parser.add_argument(
            '-c',
            '--criterion',
            action='append',
            default=[],
            choices=sorted(RUN_CRITERIA),
            help=(
                'Select only runs meeting this criterion (repeatable). '
                + ' '.join(
                    f'{name}: {criterion.help}'
                    for name, criterion in sorted(RUN_CRITERIA.items())
                )
            ),
        )
        parser.add_argument(
            '--done-after',
            type=parser_type_datetime,
            metavar='datetime',
            help=(
                'Skip runs imported successfully since this moment (YYYY-MM-DD HH:MM '
                'in the local time of the host, or with an offset, e.g. +03:00), '
                'e.g. since the previous start of the command.'
            ),
        )
        parser.add_argument(
            '--max-running',
            type=int,
            default=2,
            metavar='n',
            help=(
                'The most imports of the runs running at a time (default: 2). '
                'It is lowered to leave other imports at least one Celery worker process.'
            ),
        )
        parser.add_argument(
            '--hours',
            type=parser_type_hours,
            metavar='start-end',
            help=(
                'Schedule imports only in these hours of the local time of the host, '
                'the end excluded (e.g. 22-7).'
            ),
        )
        parser.add_argument(
            '--poll',
            type=float,
            default=30,
            metavar='seconds',
            help='Seconds between checks of the imports and the queue (default: 30).',
        )
        parser.add_argument(
            '--task-timeout',
            type=float,
            default=3,
            metavar='hours',
            help='Hours to wait for an import before going on without it (default: 3).',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Only report the runs that would be imported again.',
        )

    def handle(self, *args, **options):
        run_ids = options['id']
        run_from = options['from']
        run_to = options['to']

        if not run_ids and not run_from and not run_to:
            msg = 'Specify at least one of: -i, -f, -t.'
            raise CommandError(msg)
        if options['max_running'] < 1:
            msg = '--max-running must be at least 1.'
            raise CommandError(msg)

        runs_query = Q(test_run=None, finish__isnull=False)
        if run_ids:
            runs_query &= Q(id__in=run_ids)
        if run_from:
            runs_query &= Q(start__date__gte=run_from)
        if run_to:
            runs_query &= Q(finish__date__lte=run_to)

        runs = TestIterationResult.objects.filter(runs_query)
        for name in options['criterion']:
            runs = RUN_CRITERIA[name].filter(runs)

        skipped = 0
        if options['done_after']:
            done = imported_after(runs, options['done_after'])
            skipped = done.count()
            runs = runs.exclude(id__in=done.values('id'))

        selected = list(runs.order_by('id').values_list('id', flat=True))

        if run_ids:
            missing = set(run_ids) - set(selected)
            if missing:
                self.stdout.write(
                    self.style.WARNING(
                        f'Runs not found, unfinished or not meeting the criteria: '
                        f'{sorted(missing)}',
                    ),
                )
        if skipped:
            self.stdout.write(f'Skipped as imported since {options["done_after"]}: {skipped}')

        self.stdout.write(f'Runs to import again: {len(selected)}')

        if options['dry_run']:
            self.stdout.write(f'\t{selected}')
            return
        if not selected:
            return

        reimport = GradualReimport(
            selected,
            max_running=options['max_running'],
            hours=options['hours'],
            poll_interval=options['poll'],
            task_timeout=timedelta(hours=options['task_timeout']),
            notify=self.stdout.write,
        )
        try:
            report = reimport.run()
        except KeyboardInterrupt:
            report = reimport.report
            self.stdout.write(self.style.WARNING('\nInterrupted.'))

        self.write_summary(report)

    def write_summary(self, report):
        rows = [
            ('Imported', report.succeeded, 'SUCCESS'),
            ('Failed', report.failed, 'ERROR'),
            ('Not finished in time', report.timed_out, 'ERROR'),
            ('No logs source found', report.no_source, 'ERROR'),
            ('Still running (not waited for)', report.running, 'WARNING'),
            ('Left (not scheduled)', report.left, 'WARNING'),
        ]

        self.stdout.write('=========================================================')
        self.stdout.write('Import again summary:')
        for label, run_ids, style in rows[1:]:
            if run_ids:
                self.stdout.write(f'\n{label}:')
                self.stdout.write(getattr(self.style, style)(f'\t{run_ids}'))

        self.stdout.write('=========================================================')
        self.stdout.write('Counts:')
        col_w = max(len(label) for label, _, _ in rows)
        for label, run_ids, style in rows:
            self.stdout.write(getattr(self.style, style)(f'\t{label:<{col_w}}  {len(run_ids)}'))
        self.stdout.write('=========================================================')
