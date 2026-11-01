from django.contrib import admin

from .models import (
    AgentCompany,
    AuditLog,
    Container,
    Document,
    Importer,
    ProposedAction,
    SecurityProfile,
    Shipment,
    StaffMember,
    Stage,
    StageEvent,
    StageExplanation,
)


@admin.register(SecurityProfile)
class SecurityProfileAdmin(admin.ModelAdmin):
    fields = ('user', 'question', 'updated_at')
    readonly_fields = ('updated_at',)
    list_display = ('user', 'question', 'updated_at')

admin.site.register(AgentCompany)
admin.site.register(Importer)
admin.site.register(Shipment)
admin.site.register(Container)
admin.site.register(Stage)
admin.site.register(StageEvent)
admin.site.register(StaffMember)
admin.site.register(AuditLog)
admin.site.register(Document)
admin.site.register(ProposedAction)
admin.site.register(StageExplanation)
