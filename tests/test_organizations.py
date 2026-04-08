"""Unit tests for organization module — slugify and CRUD DB functions."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_basic_name(self):
        from app.auth.organizations import slugify

        assert slugify("My Organization") == "my-organization"

    def test_special_characters_removed(self):
        from app.auth.organizations import slugify

        # Special chars are stripped and multiple hyphens are collapsed
        assert slugify("Org @#$ Name!") == "org-name"
        assert "--" not in slugify("Org @#$ Name!")
        # Chars directly adjacent merge together
        assert slugify("Org!@#$%^&*()Name") == "orgname"

    def test_multiple_spaces_collapsed(self):
        from app.auth.organizations import slugify

        assert slugify("Org   with   spaces") == "org-with-spaces"

    def test_leading_trailing_hyphens_stripped(self):
        from app.auth.organizations import slugify

        assert slugify("  -Org Name-  ") == "org-name"

    def test_estonian_characters(self):
        from app.auth.organizations import slugify

        # Estonian special chars (õ, ä, ö, ü, š, ž) are non-ASCII, will be stripped
        assert slugify("Õigusloome Amet") == "igusloome-amet"

    def test_empty_string(self):
        from app.auth.organizations import slugify

        assert slugify("") == ""

    def test_hyphens_preserved(self):
        from app.auth.organizations import slugify

        assert slugify("my-org-name") == "my-org-name"

    def test_numbers_preserved(self):
        from app.auth.organizations import slugify

        assert slugify("Org 123") == "org-123"

    def test_multiple_hyphens_collapsed(self):
        from app.auth.organizations import slugify

        assert slugify("Org - - Name") == "org-name"


# ---------------------------------------------------------------------------
# CRUD DB functions (mocked)
# ---------------------------------------------------------------------------


class TestListOrgs:
    @patch("app.auth.organizations._connect")
    def test_returns_list_of_dicts(self, mock_connect: MagicMock):
        from app.auth.organizations import list_orgs

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            ("id-1", "Org One", "org-one", "2024-01-01"),
            ("id-2", "Org Two", "org-two", "2024-01-02"),
        ]

        result = list_orgs()
        assert len(result) == 2
        assert result[0]["name"] == "Org One"
        assert result[1]["slug"] == "org-two"

    @patch("app.auth.organizations._connect")
    def test_returns_empty_on_db_error(self, mock_connect: MagicMock):
        from app.auth.organizations import list_orgs

        mock_connect.side_effect = Exception("DB unavailable")
        result = list_orgs()
        assert result == []


class TestGetOrg:
    @patch("app.auth.organizations._connect")
    def test_returns_org_dict(self, mock_connect: MagicMock):
        from app.auth.organizations import get_org

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = (
            "id-1", "Org One", "org-one", "2024-01-01"
        )

        result = get_org("id-1")
        assert result is not None
        assert result["id"] == "id-1"
        assert result["name"] == "Org One"

    @patch("app.auth.organizations._connect")
    def test_returns_none_for_missing(self, mock_connect: MagicMock):
        from app.auth.organizations import get_org

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = None

        result = get_org("nonexistent")
        assert result is None

    @patch("app.auth.organizations._connect")
    def test_returns_none_on_db_error(self, mock_connect: MagicMock):
        from app.auth.organizations import get_org

        mock_connect.side_effect = Exception("DB unavailable")
        result = get_org("id-1")
        assert result is None


class TestCreateOrg:
    @patch("app.auth.organizations._connect")
    def test_creates_and_returns_org(self, mock_connect: MagicMock):
        from app.auth.organizations import create_org

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = (
            "new-id", "New Org", "new-org", "2024-06-01"
        )

        result = create_org("New Org", "new-org")
        assert result is not None
        assert result["name"] == "New Org"
        mock_conn.commit.assert_called_once()

    @patch("app.auth.organizations._connect")
    def test_returns_none_on_failure(self, mock_connect: MagicMock):
        from app.auth.organizations import create_org

        mock_connect.side_effect = Exception("Unique constraint violation")
        result = create_org("Dup", "dup")
        assert result is None


class TestDeleteOrg:
    @patch("app.auth.organizations.get_org_user_count")
    @patch("app.auth.organizations._connect")
    def test_deletes_empty_org(self, mock_connect: MagicMock, mock_count: MagicMock):
        from app.auth.organizations import delete_org

        mock_count.return_value = 0
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        assert delete_org("id-1") is True
        mock_conn.execute.assert_called_once()

    @patch("app.auth.organizations.get_org_user_count")
    def test_refuses_to_delete_org_with_users(self, mock_count: MagicMock):
        from app.auth.organizations import delete_org

        mock_count.return_value = 5
        assert delete_org("id-1") is False


class TestGetOrgUserCount:
    @patch("app.auth.organizations._connect")
    def test_returns_count(self, mock_connect: MagicMock):
        from app.auth.organizations import get_org_user_count

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = (3,)

        assert get_org_user_count("id-1") == 3

    @patch("app.auth.organizations._connect")
    def test_returns_zero_on_error(self, mock_connect: MagicMock):
        from app.auth.organizations import get_org_user_count

        mock_connect.side_effect = Exception("DB error")
        assert get_org_user_count("id-1") == 0
