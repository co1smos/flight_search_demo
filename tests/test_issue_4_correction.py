import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import Mock

from flight_search_demo.app import ParsedNaturalLanguageRequest, RequestParseResult, run_request


class Issue4CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.log = Path(self.directory.name) / 'reports.jsonl'
        self.criteria = ParsedNaturalLanguageRequest(
            'aeroplan', 'JFK', 'CDG', '2026-11-05', 'Business', 1, 'one_way', 70000
        )
        self.adapter = Mock()
        self.adapter.execute.return_value = {"status": "MATCH_FOUND", "detail": "fixture"}
        self.request = {'request_id': 'correction', 'original_text':
                        'Aeroplan JFK to CDG on 2026-11-05 business for one adult under 70000 points'}

    def run_parsed(self, criteria, *, program_selection='supported'):
        parser = Mock()
        parser.parse.return_value = RequestParseResult(
            [criteria], program_selection=program_selection, diagnostics={'parser': 'offline'}
        )
        with redirect_stdout(io.StringIO()):
            return run_request(request=self.request, confirmation={'confirmed': True},
                               event_log_path=self.log, parser=parser,
                               adapter_registry={'aeroplan': self.adapter},
                               current_date=date(2026, 11, 1))[0]

    def test_invalid_parser_criteria_cannot_escape_or_execute(self):
        event = self.run_parsed(replace(self.criteria, cabin='suite'))
        self.assertEqual(event['status'], 'UNSUPPORTED_REQUEST')
        self.assertIn('cabin', event['detail'])
        self.assertIsNone(event['atomic_task_id'])
        self.adapter.execute.assert_not_called()

    def test_adults_requires_an_integer_without_coercion(self):
        for value in (True, 1.0, '1'):
            with self.subTest(value=value):
                event = self.run_parsed(replace(self.criteria, adults=value))
                self.assertEqual(event['status'], 'UNSUPPORTED_REQUEST')
        self.adapter.execute.assert_not_called()

    def test_installed_sdk_serializes_interactions_contract_and_reads_outputs(self):
        import httpx
        from google import genai
        from dataclasses import asdict
        from flight_search_demo.app import GoogleGenAIRequestParser, natural_language_parse_schema

        wire = []
        def respond(request):
            wire.append(json.loads(request.content))
            return httpx.Response(200, json={
                'id': 'offline-interaction', 'status': 'completed',
                'created': '2026-11-01T00:00:00Z', 'updated': '2026-11-01T00:00:00Z',
                'outputs': [{'type': 'text', 'text': json.dumps({
                    'program_selection': 'supported',
                    'stated_program': 'Aeroplan',
                    'requests': [asdict(self.criteria)],
                })}],
            })

        with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
            with genai.Client(api_key='offline-test-only', vertexai=False,
                              http_options={'httpx_client': transport}) as client:
                parser = GoogleGenAIRequestParser(client=client, model='gemini-2.5-flash')
                with redirect_stdout(io.StringIO()):
                    events = run_request(request=self.request, confirmation={},
                                         event_log_path=self.log, parser=parser,
                                         current_date=date(2026, 11, 1))
        self.assertEqual(wire[0].get('response_mime_type'), 'application/json')
        self.assertEqual(wire[0]['response_format'], natural_language_parse_schema())
        self.assertIn('program_selection', wire[0]['input'])
        self.assertEqual(wire[0]['response_format']['properties']['program_selection']['enum'],
                         ['omitted', 'supported', 'unsupported', 'ambiguous'])
        self.assertIn('program_selection', wire[0]['response_format']['required'])
        self.assertEqual(events[0]['status'], 'CONFIRMATION_REQUIRED')
        self.assertEqual(events[0]['normalized_criteria']['maximum_points'], 70000)

    def test_unsupported_program_provenance_cannot_execute_with_confirmation(self):
        self.request['original_text'] = (
            'Use Flying Blue from JFK to CDG on 2026-11-05 business under 70000 points'
        )
        parser = Mock()
        parser.parse.return_value = RequestParseResult(
            [self.criteria],
            program_selection='unsupported',
            stated_program='Flying Blue',
        )
        with redirect_stdout(io.StringIO()):
            event = run_request(
                request=self.request,
                confirmation={'confirmed': True},
                event_log_path=self.log,
                parser=parser,
                adapter_registry={'aeroplan': self.adapter},
                current_date=date(2026, 11, 1),
            )[0]

        self.assertEqual(event['status'], 'UNSUPPORTED_REQUEST')
        self.assertIsNone(event['atomic_task_id'])
        self.adapter.execute.assert_not_called()

    def test_ambiguous_program_provenance_cannot_execute_with_confirmation(self):
        self.request['original_text'] = (
            'Find an ANA flight from JFK to CDG on 2026-11-05 business under 70000 points'
        )
        parser = Mock()
        parser.parse.return_value = RequestParseResult(
            [self.criteria],
            program_selection='ambiguous',
            stated_program='ANA flight',
        )
        with redirect_stdout(io.StringIO()):
            event = run_request(
                request=self.request,
                confirmation={'confirmed': True},
                event_log_path=self.log,
                parser=parser,
                adapter_registry={'aeroplan': self.adapter},
                current_date=date(2026, 11, 1),
            )[0]

        self.assertEqual(event['status'], 'CLARIFICATION_REQUIRED')
        self.assertIsNone(event['atomic_task_id'])
        self.adapter.execute.assert_not_called()

    def test_supported_program_provenance_must_match_aliases_and_tasks(self):
        self.request['original_text'] = (
            'Use ANA miles from JFK to CDG on 2026-11-05 business under 70000 points'
        )
        parser = Mock()
        parser.parse.return_value = RequestParseResult(
            [self.criteria],
            program_selection='supported',
            stated_program='ANA miles',
        )
        with redirect_stdout(io.StringIO()):
            event = run_request(
                request=self.request,
                confirmation={'confirmed': True},
                event_log_path=self.log,
                parser=parser,
                adapter_registry={'aeroplan': self.adapter},
                current_date=date(2026, 11, 1),
            )[0]

        self.assertEqual(event['status'], 'CLARIFICATION_REQUIRED')
        self.assertIn('program', event['detail'])
        self.assertIsNone(event['atomic_task_id'])
        self.adapter.execute.assert_not_called()

    def test_omitted_program_provenance_cannot_override_an_explicit_supported_alias(self):
        self.request['original_text'] = (
            'Use ANA miles from JFK to CDG on 2026-11-05 business under 70000 points'
        )
        parser = Mock()
        parser.parse.return_value = RequestParseResult(
            [self.criteria],
            program_selection='omitted',
        )
        with redirect_stdout(io.StringIO()):
            event = run_request(
                request=self.request,
                confirmation={'confirmed': True},
                event_log_path=self.log,
                parser=parser,
                adapter_registry={'aeroplan': self.adapter},
                current_date=date(2026, 11, 1),
            )[0]

        self.assertEqual(event['status'], 'CLARIFICATION_REQUIRED')
        self.assertIn('provenance', event['detail'])
        self.assertIsNone(event['atomic_task_id'])
        self.adapter.execute.assert_not_called()

    def test_supported_program_provenance_must_match_stated_program_text(self):
        from flight_search_demo.app import build_confirmation_request

        parser = Mock()
        parser.parse.return_value = RequestParseResult(
            [self.criteria],
            program_selection='supported',
            stated_program='ANA Mileage Club',
        )
        with redirect_stdout(io.StringIO()):
            event = run_request(
                request=self.request,
                confirmation=build_confirmation_request(
                    request=self.request, parsed_requests=[self.criteria]
                ),
                event_log_path=self.log,
                parser=parser,
                adapter_registry={'aeroplan': self.adapter},
                current_date=date(2026, 11, 1),
            )[0]

        self.assertEqual(event['status'], 'CLARIFICATION_REQUIRED')
        self.assertIn('program', event['detail'])
        self.assertIsNone(event['atomic_task_id'])
        self.adapter.execute.assert_not_called()

    def test_invalid_program_selection_provenance_is_parser_failure(self):
        parser = Mock()
        parser.parse.return_value = RequestParseResult(
            [self.criteria],
            program_selection='maybe',
        )
        with redirect_stdout(io.StringIO()):
            event = run_request(
                request=self.request,
                confirmation={'confirmed': True},
                event_log_path=self.log,
                parser=parser,
                adapter_registry={'aeroplan': self.adapter},
                current_date=date(2026, 11, 1),
            )[0]

        self.assertEqual(event['status'], 'PARSER_FAILED')
        self.assertIsNone(event['atomic_task_id'])
        self.adapter.execute.assert_not_called()

    def test_cli_allowlisted_exact_codes_execute_and_fixture_nonmatch_is_deterministic(self):
        from unittest.mock import patch
        from flight_search_demo.app import build_request_hash, main, normalize_request

        for origin, destination in (('LHR', 'NRT'), ('NRT', 'SFO'), ('SFO', 'LHR')):
            with self.subTest(origin=origin, destination=destination):
                request = {
                    'request_id': f'cli-{origin.lower()}-{destination.lower()}',
                    'original_text': f'Find Aeroplan from {origin} to {destination}',
                    'program': 'aeroplan',
                    'origin': origin,
                    'destination': destination,
                    'departure_date': '2026-11-05',
                    'cabin': 'business',
                    'adults': 1,
                    'trip_type': 'one_way',
                    'maximum_points': 70000,
                }
                criteria = normalize_request(request, current_date=date(2026, 11, 1))
                confirmation = {
                    'request_id': request['request_id'],
                    'request_hash': build_request_hash(
                        request['request_id'], request['original_text'], criteria
                    ),
                    'confirmed': True,
                }
                request_path = self.log.parent / f'{origin}-{destination}-request.json'
                confirmation_path = self.log.parent / f'{origin}-{destination}-confirmation.json'
                event_log_path = self.log.parent / f'{origin}-{destination}-events.jsonl'
                request_path.write_text(json.dumps(request), encoding='utf-8')
                confirmation_path.write_text(json.dumps(confirmation), encoding='utf-8')
                with patch('sys.argv', [
                    'flight_search_demo.app', '--request', str(request_path),
                    '--confirmation', str(confirmation_path),
                    '--event-log', str(event_log_path),
                    '--current-date', '2026-11-01',
                ]):
                    exit_code = main()

                event = json.loads(event_log_path.read_text(encoding='utf-8'))
                self.assertEqual(exit_code, 1)
                self.assertEqual(event['status'], 'NO_AWARD_AVAILABILITY')
                self.assertIsNotNone(event['atomic_task_id'])
                self.assertEqual(event['normalized_criteria']['origin'], origin)
                self.assertEqual(event['normalized_criteria']['destination'], destination)

    def test_sdk_numeric_fields_are_never_coerced(self):
        from dataclasses import asdict
        from types import SimpleNamespace
        from flight_search_demo.app import GoogleGenAIRequestParser

        for field in ('adults', 'maximum_points'):
            for value in (True, '1', 1.0):
                with self.subTest(field=field, value=value):
                    client = Mock()
                    item = asdict(replace(self.criteria, **{field: value}))
                    client.interactions.create.return_value = SimpleNamespace(outputs=[
                        SimpleNamespace(type='text', text=json.dumps({
                            'program_selection': 'supported',
                            'stated_program': 'Aeroplan',
                            'requests': [item],
                        }))])
                    parser = GoogleGenAIRequestParser(client=client, model='offline')
                    with redirect_stdout(io.StringIO()):
                        event = run_request(request=self.request, confirmation={},
                                            event_log_path=self.log, parser=parser,
                                            adapter_registry={'aeroplan': self.adapter},
                                            current_date=date(2026, 11, 1))[0]
                    self.assertEqual(event['status'], 'UNSUPPORTED_REQUEST')
        self.adapter.execute.assert_not_called()

    def test_parser_diagnostics_are_separate_and_linked_on_success_and_failure(self):
        from flight_search_demo.app import ParserFailure, build_confirmation_request
        parser = Mock()
        parser.parse.side_effect = [
            RequestParseResult([self.criteria], program_selection='supported',
                               diagnostics={'model': 'offline-model'}),
            ParserFailure('internal-sdk-detail', diagnostics={'model': 'offline-model'}),
        ]
        self.adapter.execute.return_value = {'status': 'MATCH_FOUND', 'detail': 'fixture'}
        confirmation = build_confirmation_request(request=self.request, parsed_requests=[self.criteria])
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            for _ in range(2):
                run_request(request=self.request, confirmation=confirmation, parser=parser,
                            event_log_path=self.log, adapter_registry={'aeroplan': self.adapter},
                            current_date=date(2026, 11, 1))
        reports = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([r['status'] for r in reports], ['MATCH_FOUND', 'PARSER_FAILED'])
        self.assertNotIn('offline-model', self.log.read_text() + stdout.getvalue())
        self.assertNotIn('internal-sdk-detail', self.log.read_text() + stdout.getvalue())
        diagnostics = [json.loads(line) for line in self.log.with_suffix('.diagnostics.jsonl').read_text().splitlines()]
        self.assertEqual(len(diagnostics), 2)
        for report, diagnostic in zip(reports, diagnostics):
            self.assertEqual(report['diagnostic_id'], diagnostic['diagnostic_id'])
            self.assertEqual(diagnostic['request_id'], self.request['request_id'])
            self.assertEqual(diagnostic['metadata']['model'], 'offline-model')
        self.assertEqual(diagnostics[1]['error'], 'internal-sdk-detail')

    def test_cli_parser_construction_failure_is_reported_without_execution(self):
        from unittest.mock import patch
        from flight_search_demo.app import main
        request_path = self.log.parent / 'request.json'
        confirmation_path = self.log.parent / 'confirmation.json'
        request_path.write_text(json.dumps(self.request))
        confirmation_path.write_text(json.dumps({'confirmed': True}))
        with patch('sys.argv', ['app', '--request', str(request_path), '--confirmation',
                               str(confirmation_path), '--event-log', str(self.log)]), patch(
                'flight_search_demo.app.build_default_request_parser',
                side_effect=RuntimeError('client initialization failed')), patch(
                'flight_search_demo.app.execute_confirmed_request') as execute, redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 1)
        event = json.loads(self.log.read_text())
        self.assertEqual(event['status'], 'PARSER_FAILED')
        self.assertIn('diagnostic_id', event)
        execute.assert_not_called()

    def test_malformed_parser_result_is_a_linked_nonexecuting_failure(self):
        from types import SimpleNamespace
        for result in (None, {}, RequestParseResult([{}], program_selection='supported'),
                       RequestParseResult(None, program_selection='supported'),
                       RequestParseResult([self.criteria], program_selection='supported',
                                           clarification=True),
                       RequestParseResult([SimpleNamespace(program='aeroplan')],
                                           program_selection='supported')):
            with self.subTest(result=result):
                parser = Mock()
                parser.parse.return_value = result
                with redirect_stdout(io.StringIO()):
                    event = run_request(request=self.request, confirmation={'confirmed': True},
                                        event_log_path=self.log, parser=parser,
                                        adapter_registry={'aeroplan': self.adapter},
                                        current_date=date(2026, 11, 1))[0]
                self.assertEqual(event['status'], 'PARSER_FAILED')
                self.assertIn('diagnostic_id', event)
        self.adapter.execute.assert_not_called()

    def test_unserializable_parser_diagnostics_do_not_escape_application(self):
        parser = Mock()
        parser.parse.return_value = RequestParseResult(
            [self.criteria], program_selection='supported', diagnostics={'provider': object()}
        )
        with redirect_stdout(io.StringIO()):
            event = run_request(
                request=self.request,
                confirmation={'confirmed': True},
                event_log_path=self.log,
                parser=parser,
                adapter_registry={'aeroplan': self.adapter},
                current_date=date(2026, 11, 1),
            )[0]
        self.assertEqual(event['status'], 'CONFIRMATION_REQUIRED')
        self.assertIsNone(event['atomic_task_id'])
        self.assertIn('diagnostic_id', event)
        self.adapter.execute.assert_not_called()

    def test_invalid_timezone_is_nonexecuting_and_never_reaches_parser(self):
        parser = Mock()
        parser.parse.return_value = RequestParseResult([self.criteria], program_selection='supported')
        with redirect_stdout(io.StringIO()):
            event = run_request(request=self.request, confirmation={}, event_log_path=self.log,
                                parser=parser, timezone_name='Mars/Olympus',
                                current_date=date(2026, 11, 1))[0]
        self.assertEqual(event['status'], 'UNSUPPORTED_REQUEST')
        self.assertIn('timezone', event['detail'])
        self.assertIn('diagnostic_id', event)
        parser.parse.assert_not_called()

    def test_clock_date_is_derived_in_configured_timezone_for_parse_and_validation(self):
        from datetime import datetime, timezone
        parser = Mock()
        parser.parse.return_value = RequestParseResult(
            [replace(self.criteria, departure_date='2026-11-01')],
            program_selection='supported',
        )
        for zone, expected_date, status in (
            ('America/Los_Angeles', date(2026, 11, 1), 'CONFIRMATION_REQUIRED'),
            ('Asia/Tokyo', date(2026, 11, 2), 'UNSUPPORTED_REQUEST'),
        ):
            with self.subTest(zone=zone), redirect_stdout(io.StringIO()):
                event = run_request(request=self.request, confirmation={}, event_log_path=self.log,
                                    parser=parser, timezone_name=zone,
                                    clock=lambda: datetime(2026, 11, 2, 0, 30, tzinfo=timezone.utc))[0]
                self.assertEqual(parser.parse.call_args.kwargs['current_date'], expected_date)
                self.assertEqual(event['status'], status)

    def test_ana_flight_requires_clarification_despite_overeager_parser_and_confirmation(self):
        from flight_search_demo.app import build_confirmation_request
        self.request['original_text'] = 'Find an ANA flight from JFK to CDG on 2026-11-05 business under 70000 points'
        confirmation = build_confirmation_request(request=self.request, parsed_requests=[self.criteria])
        parser = Mock()
        parser.parse.return_value = RequestParseResult([self.criteria], program_selection='ambiguous',
                                                       stated_program='ANA flight')
        with redirect_stdout(io.StringIO()):
            event = run_request(request=self.request, confirmation=confirmation, parser=parser,
                                event_log_path=self.log, adapter_registry={'aeroplan': self.adapter},
                                current_date=date(2026, 11, 1))[0]
        self.assertEqual(event['status'], 'CLARIFICATION_REQUIRED')
        self.assertIn('ANA', event['detail'])
        self.adapter.execute.assert_not_called()

    def test_ambiguous_numeric_dates_require_clarification_before_confirmation(self):
        for raw_date in ('09/07/2026', '09-07-2026', '09.07.2026', '9/7'):
            with self.subTest(raw_date=raw_date):
                self.request['original_text'] = f'Aeroplan JFK to CDG on {raw_date} business under 70000 points'
                event = self.run_parsed(self.criteria)
                self.assertEqual(event['status'], 'CLARIFICATION_REQUIRED')
                self.assertIn('date', event['detail'])
        self.adapter.execute.assert_not_called()

    def test_program_aliases_are_normalized_at_application_confirmation_seam(self):
        for alias, program in (('AC points', 'aeroplan'), ('Air Canada Aeroplan', 'aeroplan'),
                               ('Aeroplan', 'aeroplan'), ('ANA miles', 'ana'),
                               ('ANA Mileage Club', 'ana'), ('ANA', 'ana')):
            with self.subTest(alias=alias):
                self.request['original_text'] = f'Use {alias} from JFK to CDG on 2026-11-05 business under 70000'
                parser = Mock()
                parser.parse.return_value = RequestParseResult(
                    [replace(self.criteria, program=alias)],
                    program_selection='supported',
                    stated_program=alias,
                )
                with redirect_stdout(io.StringIO()):
                    event = run_request(request=self.request, confirmation={}, parser=parser,
                                        event_log_path=self.log, current_date=date(2026, 11, 1))[0]
                self.assertEqual(event['status'], 'CONFIRMATION_REQUIRED')
                self.assertEqual(event['normalized_criteria']['program'], program)

    def test_no_stated_program_defaults_to_aeroplan_after_validation(self):
        self.request['original_text'] = 'JFK to CDG on 2026-11-05 business one adult under 70000 points'
        for parsed_program in ('', 'ana'):
            with self.subTest(parsed_program=parsed_program):
                event = self.run_parsed(
                    replace(self.criteria, program=parsed_program),
                    program_selection='omitted',
                )
                self.assertEqual(event['status'], 'CONFIRMATION_REQUIRED')
                self.assertEqual(event['normalized_criteria']['program'], 'aeroplan')
        event = self.run_parsed(
            replace(self.criteria, program='', cabin=None), program_selection='omitted'
        )
        self.assertIn(event['status'], ('CLARIFICATION_REQUIRED', 'UNSUPPORTED_REQUEST'))
        self.adapter.execute.assert_not_called()

    def test_materially_ambiguous_loyalty_language_cannot_default_to_aeroplan(self):
        self.request['original_text'] = 'Find a loyalty award from JFK to CDG on 2026-11-05 business under 70000 points'
        event = self.run_parsed(self.criteria, program_selection='ambiguous')
        self.assertIn(event['status'], ('UNSUPPORTED_REQUEST', 'CLARIFICATION_REQUIRED'))
        self.assertIn('program', event['detail'])
        self.adapter.execute.assert_not_called()

    def test_parser_cannot_change_stated_program_selection(self):
        for selection in ('ANA miles', 'AC points and ANA miles'):
            with self.subTest(selection=selection):
                self.request['original_text'] = f'{selection} JFK to CDG on 2026-11-05 business under 70000 points'
                event = self.run_parsed(self.criteria, program_selection='supported')
                self.assertEqual(event['status'], 'CLARIFICATION_REQUIRED')
                self.assertIn('program', event['detail'])
        self.adapter.execute.assert_not_called()

    def test_explicit_unsupported_loyalty_language_never_defaults_to_aeroplan(self):
        for loyalty in ('United miles', 'United MileagePlus', 'Delta SkyMiles', 'Delta miles'):
            with self.subTest(loyalty=loyalty):
                self.request['original_text'] = f'Use {loyalty} JFK to CDG on 2026-11-05 business under 70000'
                event = self.run_parsed(self.criteria, program_selection='unsupported')
                self.assertEqual(event['status'], 'UNSUPPORTED_REQUEST')
                self.assertIn('program', event['detail'])
        self.adapter.execute.assert_not_called()

    def test_unknown_explicit_loyalty_programs_cannot_default_to_aeroplan(self):
        from flight_search_demo.app import build_confirmation_request

        for loyalty in (
            'Flying Blue',
            'Emirates Skywards',
            'British Airways Executive Club',
            'KrisFlyer',
            'AAdvantage',
            'Alaska miles',
            'Avios',
        ):
            with self.subTest(loyalty=loyalty):
                self.request['original_text'] = (
                    f'Use {loyalty} from JFK to CDG on 2026-11-05 '
                    'business under 70000 points'
                )
                confirmation = build_confirmation_request(
                    request=self.request,
                    parsed_requests=[self.criteria],
                    current_date=date(2026, 11, 1),
                )
                parser = Mock()
                parser.parse.return_value = RequestParseResult(
                    [self.criteria],
                    program_selection='unsupported',
                    stated_program=loyalty,
                )
                with redirect_stdout(io.StringIO()):
                    event = run_request(
                        request=self.request,
                        confirmation=confirmation,
                        event_log_path=self.log,
                        parser=parser,
                        adapter_registry={'aeroplan': self.adapter},
                        current_date=date(2026, 11, 1),
                    )[0]
                self.assertIn(event['status'], ('UNSUPPORTED_REQUEST', 'CLARIFICATION_REQUIRED'))
                self.assertIsNone(event['atomic_task_id'])
        self.adapter.execute.assert_not_called()

    def test_terminal_displays_all_program_criteria_and_clarification_detail(self):
        self.request['original_text'] = 'AC points and ANA miles JFK to CDG on 2026-11-05 business under 70000'
        parser = Mock()
        parser.parse.side_effect = [
            RequestParseResult([self.criteria, replace(self.criteria, program='ana')],
                               program_selection='supported'),
            RequestParseResult([], program_selection='ambiguous',
                               clarification='Specify an exact departure date'),
        ]
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            for _ in range(2):
                run_request(request=self.request, confirmation={}, event_log_path=self.log,
                            parser=parser, current_date=date(2026, 11, 1))
        output = stdout.getvalue()
        for program in ('aeroplan', 'ana'):
            self.assertIn(f'program={program} origin=JFK destination=CDG departure_date=2026-11-05', output)
        self.assertIn('cabin=Business adults=1 trip_type=one_way maximum_points=70000', output)
        self.assertIn('Specify an exact departure date', output)

    def test_conflicting_or_missing_shared_ceiling_cannot_execute(self):
        from flight_search_demo.app import build_confirmation_request
        cases = [
            ('Aeroplan and ANA JFK to CDG on 2026-11-05 business under 70000 points', 70000, 90000),
            ('Aeroplan under 70000 points and ANA under 90000 miles JFK to CDG on 2026-11-05 business', 70000, 70000),
            ('Aeroplan and ANA JFK to CDG on 2026-11-05 business under 70000 points', 90000, 90000),
            ('Aeroplan and ANA JFK to CDG on 2026-11-05 business', 70000, 70000),
        ]
        for text, aeroplan_cap, ana_cap in cases:
            with self.subTest(text=text, caps=(aeroplan_cap, ana_cap)):
                self.request['original_text'] = text
                criteria = [replace(self.criteria, maximum_points=aeroplan_cap),
                            replace(self.criteria, program='ana', maximum_points=ana_cap)]
                confirmation = build_confirmation_request(request=self.request, parsed_requests=criteria)
                parser = Mock()
                parser.parse.return_value = RequestParseResult(
                    criteria, program_selection='supported'
                )
                with redirect_stdout(io.StringIO()):
                    event = run_request(request=self.request, confirmation=confirmation, parser=parser,
                                        event_log_path=self.log,
                                        adapter_registry={'aeroplan': self.adapter, 'ana': self.adapter},
                                        current_date=date(2026, 11, 1))[0]
                self.assertEqual(event['status'], 'CLARIFICATION_REQUIRED')
                self.assertIn('ceiling', event['detail'])
        self.adapter.execute.assert_not_called()

    def test_multi_program_non_program_criteria_must_match_before_execution(self):
        from flight_search_demo.app import build_confirmation_request

        baseline = self.criteria
        for field, value in (
            ('origin', 'CDG'),
            ('departure_date', '2026-11-06'),
            ('cabin', 'Economy'),
        ):
            with self.subTest(field=field, value=value):
                self.request['original_text'] = (
                    'AC points and ANA miles JFK to CDG on 2026-11-05 '
                    'business under 70000 points'
                )
                mismatched = replace(baseline, program='ana', **{field: value})
                criteria = [baseline, mismatched]
                confirmation = build_confirmation_request(
                    request=self.request,
                    parsed_requests=criteria,
                    current_date=date(2026, 11, 1),
                )
                parser = Mock()
                parser.parse.return_value = RequestParseResult(
                    criteria, program_selection='supported'
                )
                self.adapter.reset_mock()
                with redirect_stdout(io.StringIO()):
                    event = run_request(
                        request=self.request,
                        confirmation=confirmation,
                        event_log_path=self.log,
                        parser=parser,
                        adapter_registry={'aeroplan': self.adapter, 'ana': self.adapter},
                        current_date=date(2026, 11, 1),
                    )[0]
                self.assertEqual(event['status'], 'CLARIFICATION_REQUIRED')
                self.assertIn('identical', event['detail'])
                self.assertIn('only program may differ', event['detail'])
                self.adapter.execute.assert_not_called()

    def test_city_expansion_requires_explicit_interpretation_confirmation(self):
        from flight_search_demo.app import build_confirmation_request
        self.request['original_text'] = 'Aeroplan from New York to Paris on 2026-11-05 business under 70000 points'
        confirmation = build_confirmation_request(request=self.request, parsed_requests=[self.criteria])
        parser = Mock()
        parser.parse.return_value = RequestParseResult([self.criteria], program_selection='supported')
        with redirect_stdout(io.StringIO()):
            first = run_request(request=self.request, confirmation=confirmation, parser=parser,
                                event_log_path=self.log, adapter_registry={'aeroplan': self.adapter},
                                current_date=date(2026, 11, 1))[0]
        self.assertEqual(first['status'], 'CLARIFICATION_REQUIRED')
        self.adapter.execute.assert_not_called()
        self.assertEqual(first['airport_expansions'], {'origin': 'JFK', 'destination': 'CDG'})
        confirmation['airport_expansions'] = first['airport_expansions']
        with redirect_stdout(io.StringIO()):
            second = run_request(request=self.request, confirmation=confirmation, parser=parser,
                                 event_log_path=self.log, adapter_registry={'aeroplan': self.adapter},
                                 current_date=date(2026, 11, 1))[0]
        self.assertEqual(second['status'], 'MATCH_FOUND')
        self.adapter.execute.assert_called_once()

    def test_duplicate_program_entries_cannot_execute_multiple_tasks(self):
        from flight_search_demo.app import build_confirmation_request
        parser = Mock()
        parser.parse.return_value = RequestParseResult(
            [self.criteria, self.criteria], program_selection='supported'
        )
        confirmation = build_confirmation_request(request=self.request, parsed_requests=[self.criteria, self.criteria])
        with redirect_stdout(io.StringIO()):
            event = run_request(request=self.request, confirmation=confirmation, parser=parser,
                                event_log_path=self.log, adapter_registry={'aeroplan': self.adapter},
                                current_date=date(2026, 11, 1))[0]
        self.assertEqual(event['status'], 'CLARIFICATION_REQUIRED')
        self.adapter.execute.assert_not_called()

    def test_invalid_points_notation_cannot_be_partially_parsed_or_raise(self):
        for notation, parsed_cap in (('70.5k', 70), ('70.5k points', 5000), ('7,0', 70),
                                     ('-70000', 70000), ('70000e2', 70000)):
            with self.subTest(notation=notation[:30], parsed_cap=parsed_cap):
                self.request['original_text'] = f'Aeroplan JFK to CDG on 2026-11-05 business under {notation}'
                event = self.run_parsed(replace(self.criteria, maximum_points=parsed_cap))
                self.assertEqual(event['status'], 'CLARIFICATION_REQUIRED')
        self.adapter.execute.assert_not_called()

    def test_supported_points_ceiling_forms_are_normalized(self):
        for notation in ('70000', '70,000', '70k', '70 K'):
            with self.subTest(notation=notation):
                self.request['original_text'] = (
                    f'Aeroplan JFK to CDG on 2026-11-05 business under {notation} points'
                )
                event = self.run_parsed(self.criteria)
                self.assertEqual(event['status'], 'CONFIRMATION_REQUIRED')
        self.adapter.execute.assert_not_called()

    def test_structured_path_derives_date_in_configured_timezone(self):
        from datetime import datetime, timezone

        request = {
            'request_id': 'structured-zone',
            'original_text': 'Aeroplan JFK to CDG on 2026-10-31 business under 70000 points',
            'program': 'aeroplan', 'origin': 'JFK', 'destination': 'CDG',
            'departure_date': '2026-10-31', 'cabin': 'business', 'adults': 1,
            'trip_type': 'one_way', 'maximum_points': 70000,
        }
        for zone, expected_status in (
            ('America/Los_Angeles', 'CONFIRMATION_REQUIRED'),
            ('Asia/Tokyo', 'UNSUPPORTED_REQUEST'),
        ):
            with self.subTest(zone=zone), redirect_stdout(io.StringIO()):
                event = run_request(
                    request=request, confirmation={}, event_log_path=self.log,
                    adapter_registry={'aeroplan': self.adapter}, timezone_name=zone,
                    clock=lambda: datetime(2026, 11, 1, 0, 30, tzinfo=timezone.utc),
                )[0]
            self.assertEqual(event['status'], expected_status)
