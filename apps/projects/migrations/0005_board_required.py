import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("projects", "0004_boards_data")]
    operations = [
        migrations.AlterField(
            model_name="revision", name="board",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="revisions", to="projects.board"),
        ),
        migrations.AlterUniqueTogether(name="revision", unique_together={("board", "name")}),
    ]
