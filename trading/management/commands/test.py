from django.core.management.commands.test import Command as DjangoTest
from django.db import connections

from config.database_guard import check_test_target


class Command(DjangoTest):
    def handle(self, *args, **options):
        for connection in connections.all():
            check_test_target(connection)
        return super().handle(*args, **options)
