"""Context data for the agent pages."""

from .models import StaffMember


def agent_identity(request):
    """The signed-in agent's company name and username.

    Empty strings for anonymous users and users without a staff
    record, so templates can hide the identity block.
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return {'agent_company_name': '', 'agent_username': ''}
    try:
        company = user.staffmember.agent_company
    except (StaffMember.DoesNotExist, AttributeError):
        return {'agent_company_name': '', 'agent_username': ''}
    return {
        'agent_company_name': company.name,
        'agent_username': user.username,
    }
