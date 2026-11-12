from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render

from .forms import ReportForm
from .models import ReportExport
from .tasks import generate_report


@login_required
def report_list(request):
    if request.method == "POST":
        form = ReportForm(request.POST)
        in_progress = ReportExport.objects.filter(
            user=request.user, status__in=[ReportExport.Status.PENDING, ReportExport.Status.PROCESSING]
        ).count()
        if in_progress >= settings.MAX_IN_PROGRESS_REPORTS:
            messages.error(request, "Забагато звітів у черзі. Дочекайтесь завершення попередніх.")
        elif form.is_valid():
            export = ReportExport.objects.create(user=request.user, **form.cleaned_data)
            generate_report.delay(export.pk)
            messages.success(request, "Звіт поставлено в чергу.")
            return redirect("report_list")
    else:
        form = ReportForm()
    exports = list(ReportExport.objects.filter(user=request.user)[:50])
    return render(
        request,
        "reports/report_list.html",
        {"form": form, "exports": exports, "refresh": any(e.in_progress for e in exports)},
    )


@login_required
def report_download(request, pk):
    # Ownership check in the query: someone else's id is a 404, and files are
    # never served from a public /media/ URL.
    export = get_object_or_404(ReportExport, pk=pk, user=request.user)
    if export.status != ReportExport.Status.DONE or not export.file:
        raise Http404
    return FileResponse(export.file.open("rb"), as_attachment=True, filename=export.file.name.rsplit("/", 1)[-1])
