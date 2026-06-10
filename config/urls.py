"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path
from simulator import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.landing_view, name='landing'),
    path('console/', views.dashboard_view, name='dashboard'),
    path('console/qa/', views.qa_suite_view, name='qa_suite'),
    path('about/', views.about_view, name='about'),
    path('parse-swagger/', views.parse_swagger, name='parse_swagger'),
    path('proxy-request/', views.proxy_request, name='proxy_request'),
    path('generate-ai-ui/', views.generate_ai_ui, name='generate_ai_ui'),
    path('analyze-auth/', views.analyze_auth, name='analyze_auth'),
    path('generate-test-plan/', views.generate_test_plan, name='generate_test_plan'),
    path('generate-test-report/', views.generate_test_report, name='generate_test_report'),
]
