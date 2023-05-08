# Django imports
from django.db.models import Q, Count
from django.db.models.functions import TruncMonth

# Third party imports
from rest_framework import status
from rest_framework.response import Response
from sentry_sdk import capture_exception

# Module imports
from plane.api.views import BaseAPIView, BaseViewSet
from plane.api.permissions import WorkSpaceAdminPermission
from plane.db.models import Issue, AnalyticView, Workspace, State, Label
from plane.api.serializers import AnalyticViewSerializer
from plane.utils.analytics_plot import build_graph_plot
from plane.bgtasks.analytic_plot_export import analytic_export_task


class AnalyticsEndpoint(BaseAPIView):
    permission_classes = [
        WorkSpaceAdminPermission,
    ]

    def get(self, request, slug):
        try:
            x_axis = request.GET.get("x_axis", False)
            y_axis = request.GET.get("y_axis", False)

            if not x_axis or not y_axis:
                return Response(
                    {"error": "x-axis and y-axis dimensions are required"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            project_ids = request.GET.getlist("project")
            cycle_ids = request.GET.getlist("cycle")
            module_ids = request.GET.getlist("module")

            segment = request.GET.get("segment", False)

            queryset = Issue.objects.filter(workspace__slug=slug)
            if project_ids:
                queryset = queryset.filter(project_id__in=project_ids)
            if cycle_ids:
                queryset = queryset.filter(issue_cycle__cycle_id__in=cycle_ids)
            if module_ids:
                queryset = queryset.filter(issue_module__module_id__in=module_ids)

            total_issues = queryset.count()
            distribution = build_graph_plot(
                queryset=queryset, x_axis=x_axis, y_axis=y_axis, segment=segment
            )

            colors = dict()
            if x_axis in ["state__name", "state__group"]:
                key = "name" if x_axis == "state__name" else "group"
                colors = (
                    State.objects.filter(
                        workspace__slug=slug, project_id__in=project_ids
                    ).values(key, "color")
                    if project_ids
                    else State.objects.filter(workspace__slug=slug).values(key, "color")
                )

            if x_axis in ["labels__name"]:
                colors = (
                    Label.objects.filter(
                        workspace__slug=slug, project_id__in=project_ids
                    ).values("name", "color")
                    if project_ids
                    else Label.objects.filter(workspace__slug=slug).values(
                        "name", "color"
                    )
                )

            return Response(
                {
                    "total": total_issues,
                    "distribution": distribution,
                    "extras": {"colors": colors},
                },
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            capture_exception(e)
            return Response(
                {"error": "Something went wrong please try again later"},
                status=status.HTTP_400_BAD_REQUEST,
            )


class AnalyticViewViewset(BaseViewSet):
    permission_classes = [
        WorkSpaceAdminPermission,
    ]
    model = AnalyticView
    serializer_class = AnalyticViewSerializer

    def perform_create(self, serializer):
        workspace = Workspace.objects.get(slug=self.kwargs.get("slug"))
        serializer.save(workspace_id=workspace.id)

    def get_queryset(self):
        return self.filter_queryset(
            super().get_queryset().filter(workspace__slug=self.kwargs.get("slug"))
        )


class SavedAnalyticEndpoint(BaseAPIView):
    permission_classes = [
        WorkSpaceAdminPermission,
    ]

    def get(self, request, slug, analytic_id):
        try:
            analytic_view = AnalyticView.objects.get(
                pk=analytic_id, workspace__slug=slug
            )

            filter = analytic_view.query
            queryset = Issue.objects.filter(**filter)

            x_axis = analytic_view.query_dict.get("x_axis", False)
            y_axis = analytic_view.query_dict.get("y_axis", False)

            if not x_axis or not y_axis:
                return Response(
                    {"error": "x-axis and y-axis dimensions are required"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            segment = request.GET.get("segment", False)
            distribution = build_graph_plot(
                queryset=queryset, x_axis=x_axis, y_axis=y_axis, segment=segment
            )
            total_issues = queryset.count()
            return Response(
                {"total": total_issues, "distribution": distribution},
                status=status.HTTP_200_OK,
            )

        except AnalyticView.DoesNotExist:
            return Response(
                {"error": "Analytic View Does not exist"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as e:
            capture_exception(e)
            return Response(
                {"error": "Something went wrong please try again later"},
                status=status.HTTP_400_BAD_REQUEST,
            )


class ExportAnalyticsEndpoint(BaseAPIView):
    permission_classes = [
        WorkSpaceAdminPermission,
    ]

    def post(self, request, slug):
        try:
            x_axis = request.data.get("x_axis", False)
            y_axis = request.data.get("y_axis", False)

            if not x_axis or not y_axis:
                return Response(
                    {"error": "x-axis and y-axis dimensions are required"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            analytic_export_task.delay(
                email=request.user.email, data=request.data, slug=slug
            )

            return Response(
                {
                    "message": f"Once the export is ready it will be emailed to you at {str(request.user.email)}"
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            capture_exception(e)
            return Response(
                {"error": "Something went wrong please try again later"},
                status=status.HTTP_400_BAD_REQUEST,
            )


class DefaultAnalyticsEndpoint(BaseAPIView):
    def get(self, request, slug):
        try:
            queryset = Issue.objects.filter(workspace__slug=slug)

            project_ids = request.GET.getlist("project")
            cycle_ids = request.GET.getlist("cycle")
            module_ids = request.GET.getlist("module")

            if project_ids:
                queryset = queryset.filter(project_id__in=project_ids)
            if cycle_ids:
                queryset = queryset.filter(issue_cycle__cycle_id__in=cycle_ids)
            if module_ids:
                queryset = queryset.filter(issue_module__module_id__in=module_ids)

            total_issues = queryset.count()

            open_issues = queryset.filter(
                state__group__in=["backlog", "unstarted", "started"]
            ).count()

            issue_completed_month_wise = (
                queryset.filter(completed_at__isnull=False)
                .annotate(month=TruncMonth("completed_at"))
                .values("month")
                .annotate(count=Count("*"))
                .order_by("month")
            )

            most_issue_created_user = (
                queryset.filter(created_by__isnull=False).values("created_by__email")
                .annotate(record_count=Count("id"))
                .order_by("-record_count")
            )

            return Response(
                {
                    "total_issues": total_issues,
                    "open_issues": open_issues,
                    "issue_completed_month_wise": issue_completed_month_wise,
                    "most_issue_created_user": most_issue_created_user,
                },
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            print(e)
            return Response(
                {"error": "Something went wrong please try again later"},
                status=status.HTTP_400_BAD_REQUEST,
            )