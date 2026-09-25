from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class TimeEntry(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="time_entries")
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="time_entries")
    task = models.ForeignKey("projects.Task", null=True, blank=True, on_delete=models.SET_NULL, related_name="time_entries")
    date = models.DateField()
    hours = models.DecimalField(max_digits=4, decimal_places=2,
                                validators=[MinValueValidator(Decimal("0.25")), MaxValueValidator(Decimal("16"))])
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name_plural = "time entries"

    def __str__(self):
        return f"{self.user} {self.date} {self.hours}h"
