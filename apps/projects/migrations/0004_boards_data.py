"""Give every existing project a 'Main board' and move its revisions onto it."""
from django.db import migrations


def forwards(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    Board = apps.get_model("projects", "Board")
    Revision = apps.get_model("projects", "Revision")
    for project in Project.objects.all():
        board = Board.objects.filter(project=project).order_by("order", "id").first()
        if board is None:
            board = Board.objects.create(project=project, name="Main board", kind="main")
        Revision.objects.filter(project=project, board__isnull=True).update(board=board)


class Migration(migrations.Migration):
    dependencies = [("projects", "0003_boards")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
