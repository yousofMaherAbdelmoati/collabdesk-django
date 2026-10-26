from django.test import TestCase, override_settings


class DemoUiTests(TestCase):
    def test_root_serves_authenticated_demo_shell_with_workflow_controls(self):
        response = self.client.get("/")

        assert response.status_code == 200
        page = response.content.decode()
        assert "CollabDesk local demo" in page
        assert 'id="login-form"' in page
        assert "WebSocket" in page
        assert "Start stream" in page
        assert "Reports &amp; cache" in page
        assert 'id="dev-test-panel"' in page
        # The panel invokes the normal ticket endpoints; it is not an
        # unauthenticated or privileged server-side test endpoint.
        assert "/api/tickets" in page

    @override_settings(LOCAL_DEMO_SEED_ENABLED=False)
    def test_production_like_render_omits_local_test_panel(self):
        response = self.client.get("/")

        assert response.status_code == 200
        assert 'id="dev-test-panel"' not in response.content.decode()
