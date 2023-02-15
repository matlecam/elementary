import json
import re
from collections import defaultdict
from datetime import datetime
from typing import List, Optional, Tuple

from alive_progress import alive_it

from elementary.clients.slack.schema import SlackMessageSchema
from elementary.config.config import Config
from elementary.monitor.alerts.alert import Alert
from elementary.monitor.alerts.alerts import Alerts, GroupOfAlerts, GroupingType
from elementary.monitor.alerts.model import ModelAlert
from elementary.monitor.alerts.source_freshness import SourceFreshnessAlert
from elementary.monitor.alerts.test import TestAlert
from elementary.monitor.data_monitoring.data_monitoring import DataMonitoring
from elementary.monitor.fetchers.alerts.alerts import AlertsFetcher
from elementary.tracking.anonymous_tracking import AnonymousTracking
from elementary.utils.json_utils import prettify_json_str_set
from elementary.utils.log import get_logger

logger = get_logger(__name__)

YAML_FILE_EXTENSION = ".yml"
SQL_FILE_EXTENSION = ".sql"


class DataMonitoringAlerts(DataMonitoring):
    def __init__(
            self,
            config: Config,
            tracking: AnonymousTracking,
            filter: Optional[str] = None,
            force_update_dbt_package: bool = False,
            disable_samples: bool = False,
            send_test_message_on_success: bool = False,
    ):
        super().__init__(
            config, tracking, force_update_dbt_package, disable_samples, filter
        )

        self.alerts_fetcher = AlertsFetcher(
            self.internal_dbt_runner,
            self.config,
            self.elementary_database_and_schema,
        )
        self.sent_alert_count = 0
        self.send_test_message_on_success = send_test_message_on_success

    def _parse_emails_to_ids(self, emails: List[str]) -> str:
        def _regex_match_owner_email(potential_email: str) -> bool:
            email_regex = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"

            return re.fullmatch(email_regex, potential_email)

        def _get_user_id(email: str) -> str:
            user_id = self.slack_client.get_user_id_from_email(email)
            return f"<@{user_id}>" if user_id else email

        if isinstance(emails, list) and emails != []:
            ids = [
                _get_user_id(email) if _regex_match_owner_email(email) else email
                for email in emails
            ]
            parsed_ids_str = prettify_json_str_set(ids)
            return parsed_ids_str
        else:
            return prettify_json_str_set(emails)

    def _fix_owners_and_subscribers(self, group_alert: GroupOfAlerts):
        """
        goes to the slack API and gets back the handle for owners, subscribers.
        :param group_alert:
        :return:
        """
        for alert in group_alert.alerts:
            alert.owners = self._parse_emails_to_ids(alert.owners)
            alert.subscribers = self._parse_emails_to_ids(alert.subscribers)
        all_owners = set([])
        all_subscribers = set([])
        for alert in group_alert.alerts:
            all_owners.update(alert.owners)
            all_subscribers.update(alert.subscribers)
        group_alert.owners = all_owners
        group_alert.subscribers = all_subscribers

    def _group_alerts_per_config(self, alerts: List[Alert]) -> List[GroupOfAlerts]:
        """
        reads self.config and alerts' config, and groups alerts in a smart way
        TODO - add business logic

        :param alerts:
        :return:
        """
        return [GroupOfAlerts(alerts=[al],
                              grouping_type=GroupingType.BY_ALERT,
                              owners=al.owners if al.owners else [],
                              subscribers=al.subscribers if al.subscribers else [],
                              channel_destination=self.config.slack_channel_name,
                              )
                for al in alerts]

    def _alert_group_to_message(self, alert_group: GroupOfAlerts):
        if alert_group.grouping_type == GroupingType.BY_ALERT:
            return alert_group.alerts[0].to_slack()
        raise NotImplementedError  # TODO implement ...

    def _send_test_message(self):
        self.slack_client.send_message(
            channel_name=self.config.slack_channel_name,
            message=SlackMessageSchema(
                text=f"Elementary monitor ran successfully on {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            ),
        )
        logger.info("Sent the test message.")

    def _send_alerts(self, alerts: Alerts, dont_update_as_sent=True):
        all_alerts_to_send = alerts.get_all()
        # TODO when pushing this to master, dont_update_as_sent should default to FALSE
        if not all_alerts_to_send:
            self.execution_properties["sent_alert_count"] = self.sent_alert_count
            return

        sent_alert_ids_and_tables: List[Tuple[str, str]] = []

        alerts_groups: List[GroupOfAlerts] = self._group_alerts_per_config(all_alerts_to_send)
        alerts_with_progress_bar = alive_it(alerts_groups, title="Sending alerts")
        for alert_group in alerts_with_progress_bar:
            self._fix_owners_and_subscribers(alert_group)

            alert_msg = self._alert_group_to_message(alert_group)
            sent_successfully = self.slack_client.send_message(
                channel_name=alert_group.channel_destination,
                message=alert_msg,
            )
            alerts_ids_and_tables = [(alert.id, alert.alerts_table) for alert in alert_group.alerts]
            if sent_successfully:
                sent_alert_ids_and_tables.extend(alerts_ids_and_tables)
            else:
                logger.error(
                    f"Could not send the alert[s] - {[alert_id_and_table[0] for alert_id_and_table in alerts_ids_and_tables]}. Full alert: {json.dumps(dict(alert_msg))}"
                )
                self.success = False
        if not dont_update_as_sent:
            table_name_to_alert_ids = defaultdict(lambda: [])
            for alert_id, table_name in sent_alert_ids_and_tables:
                table_name_to_alert_ids[table_name].append(alert_id)

            for table_name, alert_ids in table_name_to_alert_ids.items():
                self.alerts_fetcher.update_sent_alerts(alert_ids, table_name)
        self.sent_alert_count += len(sent_alert_ids_and_tables)

        self.execution_properties["sent_alert_count"] = self.sent_alert_count

    def _skip_alerts(self, alerts: Alerts):
        self.alerts_fetcher.skip_alerts(
            alerts.tests.get_alerts_to_skip(), TestAlert.TABLE_NAME
        )
        self.alerts_fetcher.skip_alerts(
            alerts.models.get_alerts_to_skip(), ModelAlert.TABLE_NAME
        )
        self.alerts_fetcher.skip_alerts(
            alerts.source_freshnesses.get_alerts_to_skip(),
            SourceFreshnessAlert.TABLE_NAME,
        )

    def run_alerts(
            self,
            days_back: int,
            dbt_full_refresh: bool = False,
            dbt_vars: Optional[dict] = None,
    ) -> bool:
        logger.info("Running internal dbt run to aggregate alerts")
        # import pdb; pdb.set_trace()
        success = self.internal_dbt_runner.run(
            models="alerts", full_refresh=dbt_full_refresh, vars=dbt_vars
        )
        self.execution_properties["alerts_run_success"] = success
        if not success:
            logger.info("Could not aggregate alerts successfully")
            self.success = False
            self.execution_properties["success"] = self.success
            return self.success

        alerts = self.alerts_fetcher.get_new_alerts(
            days_back,
            disable_samples=self.disable_samples,
            filter=self.filter.get_filter(),
        )
        import pdb;
        pdb.set_trace()
        self.execution_properties[
            "elementary_test_count"
        ] = alerts.get_elementary_test_count()
        self.execution_properties["alert_count"] = alerts.count
        malformed_alert_count = alerts.malformed_count
        if malformed_alert_count > 0:
            self.success = False
        self.execution_properties["malformed_alert_count"] = malformed_alert_count
        self.execution_properties["has_subscribers"] = any(
            alert.subscribers for alert in alerts.get_all()
        )
        self._skip_alerts(alerts)
        self._send_alerts(alerts)
        if self.send_test_message_on_success and alerts.count == 0:
            self._send_test_message()
        self.execution_properties["run_end"] = True
        self.execution_properties["success"] = self.success
        return self.success
