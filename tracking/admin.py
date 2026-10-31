from django.contrib import admin

from .models import (
    AgentCompany,
    AuditLog,
    Container,
    Document,
    Importer,
    ProposedAction,
    Shipment,
    StaffMember,
    Stage,
    StageEvent,
)

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
