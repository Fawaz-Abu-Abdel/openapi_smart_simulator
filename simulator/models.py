from django.db import models

class ScannedHistory(models.Model):
    session_key = models.CharField(max_length=40, null=True, blank=True, db_index=True)
    url = models.URLField()
    title = models.CharField(max_length=255, default="Unnamed API")
    scanned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-scanned_at']
        unique_together = ['session_key', 'url']

    def __str__(self):
        return f"{self.title} ({self.url})"

class SiteStats(models.Model):
    total_visitors = models.PositiveIntegerField(default=0)
    total_requests_made = models.PositiveIntegerField(default=0)
    
    @classmethod
    def get_stats(cls):
        obj, created = cls.objects.get_or_create(id=1)
        return obj
