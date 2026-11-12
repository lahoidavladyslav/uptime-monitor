import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from api.api import api

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "openapi.json"


def render() -> str:
    """Deterministic: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(api.get_openapi_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


class Command(BaseCommand):
    help = "Write api/openapi.json (the contract the frontend types are generated from)."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Fail if the file differs from the code.")
        parser.add_argument("--path", default=str(DEFAULT_PATH))

    def handle(self, *args, **options):
        path = Path(options["path"])
        text = render()
        if options["check"]:
            if not path.exists() or path.read_text() != text:
                raise CommandError(f"{path} is stale. Run: python manage.py export_openapi (then npm run gen:api)")
            self.stdout.write(self.style.SUCCESS("OpenAPI snapshot is up to date."))
            return
        path.write_text(text)
        self.stdout.write(self.style.SUCCESS(f"Wrote {path}"))
