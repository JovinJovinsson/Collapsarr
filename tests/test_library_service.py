"""Service-layer tests for Library sync, Tracked resolution, and cascade (COL-98/COL-99).

Use the schema-initialised ``session`` fixture (no HTTP app). A Sonarr/Radarr
:class:`~collapsarr.arr.models.ArrInstance` is seeded directly, then
:class:`~collapsarr.arr.catalog.SonarrCatalog`/:class:`~collapsarr.arr.catalog.RadarrCatalog`
DTOs are built in-memory and fed to :func:`~collapsarr.library.service.sync_library`
-- no network.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from collapsarr.arr.catalog import (
    CatalogEpisode,
    CatalogMovie,
    CatalogSeries,
    RadarrCatalog,
    SonarrCatalog,
)
from collapsarr.arr.models import ArrInstance, InstanceType
from collapsarr.library.models import LibraryNode, LibraryNodeKind, make_node_key
from collapsarr.library.service import (
    build_movie_tree,
    build_tree,
    get_node_by_source_id,
    list_nodes,
    resolve_tracked,
    set_tracked,
    sync_library,
    upsert_movie_node,
    upsert_series_episode_node,
)
from collapsarr.settings.service import get_global_settings, update_global_settings


def _seed_instance(
    session: Session, *, type_: InstanceType = InstanceType.SONARR, name: str | None = None
) -> ArrInstance:
    instance = ArrInstance(
        name=name or f"Instance {type_.value}",
        type=type_,
        base_url="http://arr.local:8989",
        api_key="k",
    )
    session.add(instance)
    session.commit()
    session.refresh(instance)
    return instance


def _catalog(instance_id: int) -> SonarrCatalog:
    return SonarrCatalog(
        instance_id=instance_id,
        series=(
            CatalogSeries(
                series_id=1,
                title="Breaking Bad",
                season_numbers=(1, 2),
                episodes=(
                    CatalogEpisode(101, 1, 1, "Pilot", has_file=True),
                    CatalogEpisode(102, 1, 2, "Cat's in the Bag", has_file=False),
                    CatalogEpisode(201, 2, 1, "Seven Thirty-Seven", has_file=False),
                ),
            ),
        ),
    )


def _node(session: Session, instance_id: int, node_key: str) -> LibraryNode:
    match = next(n for n in list_nodes(session, instance_id) if n.node_key == node_key)
    return match


def _series(session: Session, instance_id: int) -> LibraryNode:
    return _node(session, instance_id, make_node_key(LibraryNodeKind.SERIES, series_id=1))


def _season(session: Session, instance_id: int, number: int) -> LibraryNode:
    key = make_node_key(LibraryNodeKind.SEASON, series_id=1, season_number=number)
    return _node(session, instance_id, key)


def _episode(session: Session, instance_id: int, episode_id: int) -> LibraryNode:
    key = make_node_key(LibraryNodeKind.EPISODE, series_id=1, episode_id=episode_id)
    return _node(session, instance_id, key)


# --- sync --------------------------------------------------------------------


def test_sync_upserts_full_catalog_including_files_not_present(session: Session) -> None:
    instance = _seed_instance(session)
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))

    nodes = list_nodes(session, instance.id)
    kinds = sorted(node.kind for node in nodes)
    # 1 series + 2 seasons + 3 episodes.
    assert kinds.count(LibraryNodeKind.SERIES) == 1
    assert kinds.count(LibraryNodeKind.SEASON) == 2
    assert kinds.count(LibraryNodeKind.EPISODE) == 3

    ep_no_file = _episode(session, instance.id, 102)
    assert ep_no_file.has_file is False  # included despite no file


def test_sync_is_idempotent(session: Session) -> None:
    instance = _seed_instance(session)
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))
    first = len(list_nodes(session, instance.id))
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))
    assert len(list_nodes(session, instance.id)) == first


# --- Tracked resolution ------------------------------------------------------


def test_resolve_defaults_to_global_default_tracked(session: Session) -> None:
    instance = _seed_instance(session)
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))
    nodes = {n.id: n for n in list_nodes(session, instance.id)}
    episode = _episode(session, instance.id, 101)

    # Global default_tracked defaults to True.
    assert resolve_tracked(episode, nodes, default_tracked=True) is True
    assert resolve_tracked(episode, nodes, default_tracked=False) is False


def test_resolve_uses_nearest_ancestor_override(session: Session) -> None:
    instance = _seed_instance(session)
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))

    series = _series(session, instance.id)
    set_tracked(session, node_id=series.id, tracked=False)

    nodes = {n.id: n for n in list_nodes(session, instance.id)}
    episode = _episode(session, instance.id, 101)
    # Series override False beats the global default True.
    assert resolve_tracked(episode, nodes, default_tracked=True) is False


def test_own_override_wins_over_ancestor(session: Session) -> None:
    instance = _seed_instance(session)
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))

    series = _series(session, instance.id)
    set_tracked(session, node_id=series.id, tracked=False)
    episode = _episode(session, instance.id, 101)
    set_tracked(session, node_id=episode.id, tracked=True)

    nodes = {n.id: n for n in list_nodes(session, instance.id)}
    refreshed = nodes[episode.id]
    assert resolve_tracked(refreshed, nodes, default_tracked=True) is True


# --- cascade -----------------------------------------------------------------


def test_set_tracked_on_series_cascades_to_all_descendants(session: Session) -> None:
    instance = _seed_instance(session)
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))

    series = _series(session, instance.id)
    set_tracked(session, node_id=series.id, tracked=False)

    for node in list_nodes(session, instance.id):
        assert node.tracked_override is False, node


def test_set_tracked_on_season_cascades_only_within_that_season(session: Session) -> None:
    instance = _seed_instance(session)
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))

    season1 = _season(session, instance.id, 1)
    set_tracked(session, node_id=season1.id, tracked=False)

    ep_s1 = _episode(session, instance.id, 101)
    ep_s2 = _episode(session, instance.id, 201)
    assert ep_s1.tracked_override is False  # under season 1
    assert ep_s2.tracked_override is None  # season 2 untouched


# --- soft-hide ---------------------------------------------------------------


def test_disappeared_node_is_hidden_then_reappears_with_override(session: Session) -> None:
    instance = _seed_instance(session)
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))

    episode = _episode(session, instance.id, 201)
    set_tracked(session, node_id=episode.id, tracked=False)

    # A later catalog that drops episode 201 (and its now-empty season 2).
    reduced = SonarrCatalog(
        instance_id=instance.id,
        series=(
            CatalogSeries(
                series_id=1,
                title="Breaking Bad",
                season_numbers=(1,),
                episodes=(
                    CatalogEpisode(101, 1, 1, "Pilot", has_file=True),
                    CatalogEpisode(102, 1, 2, "Cat's in the Bag", has_file=False),
                ),
            ),
        ),
    )
    sync_library(session, instance_id=instance.id, catalog=reduced)

    hidden = _episode(session, instance.id, 201)
    assert hidden.hidden is True
    assert hidden.tracked_override is False  # preserved, not deleted

    # It reappears in a later scan with the same override.
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))
    back = _episode(session, instance.id, 201)
    assert back.hidden is False
    assert back.tracked_override is False


# --- tree read ---------------------------------------------------------------


def test_build_tree_shape_and_resolved_values(session: Session) -> None:
    instance = _seed_instance(session)
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))
    update_global_settings(session, default_tracked=True)

    tree = build_tree(session, instance.id)
    assert tree.instance_id == instance.id
    assert len(tree.series) == 1
    series = tree.series[0]
    assert series.title == "Breaking Bad"
    assert series.tracked is True  # from global default
    assert [s.season_number for s in series.seasons] == [1, 2]
    assert [e.episode_number for e in series.seasons[0].episodes] == [1, 2]
    assert all(e.tracked is True for s in series.seasons for e in s.episodes)


def test_build_tree_respects_default_tracked_false(session: Session) -> None:
    instance = _seed_instance(session)
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))
    update_global_settings(session, default_tracked=False)

    tree = build_tree(session, instance.id)
    assert tree.series[0].tracked is False


def test_build_tree_excludes_hidden_nodes(session: Session) -> None:
    instance = _seed_instance(session)
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))
    reduced = SonarrCatalog(
        instance_id=instance.id,
        series=(
            CatalogSeries(
                series_id=1,
                title="Breaking Bad",
                season_numbers=(1,),
                episodes=(CatalogEpisode(101, 1, 1, "Pilot", has_file=True),),
            ),
        ),
    )
    sync_library(session, instance_id=instance.id, catalog=reduced)

    tree = build_tree(session, instance.id)
    season_numbers = [s.season_number for s in tree.series[0].seasons]
    assert season_numbers == [1]  # season 2 hidden
    ep_ids = {e.sonarr_episode_id for s in tree.series[0].seasons for e in s.episodes}
    assert ep_ids == {101}  # 102, 201 hidden


@pytest.mark.parametrize("field", ["sonarr_episode_id", "season_number", "episode_number"])
def test_build_tree_raises_when_episode_id_field_is_null(session: Session, field: str) -> None:
    """An Episode node missing an Arr-instance id/number is a data-integrity bug,
    not a legitimate "unknown" value -- build_tree must fail loudly (COL-99
    follow-up) rather than silently render it as ``0``.
    """
    instance = _seed_instance(session)
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))

    episode = _episode(session, instance.id, 101)
    setattr(episode, field, None)
    session.commit()

    with pytest.raises(AssertionError, match=f"{field} is NULL"):
        build_tree(session, instance.id)


def test_build_tree_raises_when_season_number_is_null(session: Session) -> None:
    instance = _seed_instance(session)
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))

    season = _season(session, instance.id, 1)
    season.season_number = None
    session.commit()

    with pytest.raises(AssertionError, match="season_number is NULL"):
        build_tree(session, instance.id)


def test_default_tracked_is_true_on_fresh_settings(session: Session) -> None:
    assert get_global_settings(session).default_tracked is True


# --- Radarr / Movie (COL-99) --------------------------------------------------


def _radarr_catalog(instance_id: int) -> RadarrCatalog:
    return RadarrCatalog(
        instance_id=instance_id,
        movies=(
            CatalogMovie(movie_id=1, title="Arrival", has_file=True),
            CatalogMovie(movie_id=2, title="Not Yet Downloaded", has_file=False),
        ),
    )


def _movie(session: Session, instance_id: int, movie_id: int) -> LibraryNode:
    key = make_node_key(LibraryNodeKind.MOVIE, movie_id=movie_id)
    return _node(session, instance_id, key)


def test_sync_upserts_full_movie_catalog_including_files_not_present(session: Session) -> None:
    instance = _seed_instance(session, type_=InstanceType.RADARR)
    sync_library(session, instance_id=instance.id, catalog=_radarr_catalog(instance.id))

    nodes = list_nodes(session, instance.id)
    assert len(nodes) == 2
    assert all(n.kind is LibraryNodeKind.MOVIE for n in nodes)
    assert all(n.parent_id is None for n in nodes)  # flat -- no ancestor level

    no_file = _movie(session, instance.id, 2)
    assert no_file.has_file is False  # included despite no file


def test_movie_sync_is_idempotent(session: Session) -> None:
    instance = _seed_instance(session, type_=InstanceType.RADARR)
    sync_library(session, instance_id=instance.id, catalog=_radarr_catalog(instance.id))
    first = len(list_nodes(session, instance.id))
    sync_library(session, instance_id=instance.id, catalog=_radarr_catalog(instance.id))
    assert len(list_nodes(session, instance.id)) == first


def test_movie_tracked_resolves_from_own_override_or_global_default(session: Session) -> None:
    instance = _seed_instance(session, type_=InstanceType.RADARR)
    sync_library(session, instance_id=instance.id, catalog=_radarr_catalog(instance.id))
    nodes = {n.id: n for n in list_nodes(session, instance.id)}
    movie = _movie(session, instance.id, 1)

    # No ancestor level -- resolves straight to the global default.
    assert resolve_tracked(movie, nodes, default_tracked=True) is True
    assert resolve_tracked(movie, nodes, default_tracked=False) is False

    set_tracked(session, node_id=movie.id, tracked=False)
    nodes = {n.id: n for n in list_nodes(session, instance.id)}
    refreshed = nodes[movie.id]
    # Own explicit override wins over the global default.
    assert resolve_tracked(refreshed, nodes, default_tracked=True) is False


def test_movie_soft_hide_then_reappears_with_override_preserved(session: Session) -> None:
    instance = _seed_instance(session, type_=InstanceType.RADARR)
    sync_library(session, instance_id=instance.id, catalog=_radarr_catalog(instance.id))

    movie = _movie(session, instance.id, 2)
    set_tracked(session, node_id=movie.id, tracked=False)

    # A later catalog that drops movie 2.
    reduced = RadarrCatalog(
        instance_id=instance.id,
        movies=(CatalogMovie(movie_id=1, title="Arrival", has_file=True),),
    )
    sync_library(session, instance_id=instance.id, catalog=reduced)

    hidden = _movie(session, instance.id, 2)
    assert hidden.hidden is True
    assert hidden.tracked_override is False  # preserved, not deleted

    # It reappears in a later scan with the same override.
    sync_library(session, instance_id=instance.id, catalog=_radarr_catalog(instance.id))
    back = _movie(session, instance.id, 2)
    assert back.hidden is False
    assert back.tracked_override is False


def test_build_movie_tree_shape_and_resolved_values(session: Session) -> None:
    instance = _seed_instance(session, type_=InstanceType.RADARR)
    sync_library(session, instance_id=instance.id, catalog=_radarr_catalog(instance.id))
    update_global_settings(session, default_tracked=True)

    tree = build_movie_tree(session, instance.id)
    assert tree.instance_id == instance.id
    assert [m.title for m in tree.movies] == ["Arrival", "Not Yet Downloaded"]  # by title
    assert all(m.tracked is True for m in tree.movies)
    arrival = next(m for m in tree.movies if m.radarr_movie_id == 1)
    assert arrival.has_file is True


def test_build_movie_tree_excludes_hidden_movies(session: Session) -> None:
    instance = _seed_instance(session, type_=InstanceType.RADARR)
    sync_library(session, instance_id=instance.id, catalog=_radarr_catalog(instance.id))
    reduced = RadarrCatalog(
        instance_id=instance.id,
        movies=(CatalogMovie(movie_id=1, title="Arrival", has_file=True),),
    )
    sync_library(session, instance_id=instance.id, catalog=reduced)

    tree = build_movie_tree(session, instance.id)
    assert {m.radarr_movie_id for m in tree.movies} == {1}  # movie 2 hidden


# --- get_node_by_source_id (COL-101) ------------------------------------------


def test_get_node_by_source_id_finds_the_matching_episode(session: Session) -> None:
    instance = _seed_instance(session)
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))
    expected = _episode(session, instance.id, 101)

    found = get_node_by_source_id(session, instance_id=instance.id, sonarr_episode_id=101)

    assert found is not None
    assert found.id == expected.id


def test_get_node_by_source_id_finds_the_matching_movie(session: Session) -> None:
    instance = _seed_instance(session, type_=InstanceType.RADARR)
    sync_library(session, instance_id=instance.id, catalog=_radarr_catalog(instance.id))
    expected = _movie(session, instance.id, 1)

    found = get_node_by_source_id(session, instance_id=instance.id, radarr_movie_id=1)

    assert found is not None
    assert found.id == expected.id


def test_get_node_by_source_id_scopes_by_instance(session: Session) -> None:
    """Two Sonarr instances that happen to reuse the same episode id don't collide."""
    instance_a = _seed_instance(session, name="Instance A")
    sync_library(session, instance_id=instance_a.id, catalog=_catalog(instance_a.id))
    instance_b = _seed_instance(session, name="Instance B")
    sync_library(session, instance_id=instance_b.id, catalog=_catalog(instance_b.id))

    found_a = get_node_by_source_id(session, instance_id=instance_a.id, sonarr_episode_id=101)
    found_b = get_node_by_source_id(session, instance_id=instance_b.id, sonarr_episode_id=101)

    assert found_a is not None
    assert found_b is not None
    assert found_a.id != found_b.id
    assert found_a.instance_id == instance_a.id
    assert found_b.instance_id == instance_b.id


def test_get_node_by_source_id_returns_none_for_unknown_id(session: Session) -> None:
    instance = _seed_instance(session)
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))

    assert get_node_by_source_id(session, instance_id=instance.id, sonarr_episode_id=999) is None


def test_get_node_by_source_id_returns_none_when_neither_id_given(session: Session) -> None:
    instance = _seed_instance(session)
    sync_library(session, instance_id=instance.id, catalog=_catalog(instance.id))

    assert get_node_by_source_id(session, instance_id=instance.id) is None


# --- incremental webhook upsert (COL-102) ------------------------------------


def test_upsert_series_episode_node_creates_the_full_ancestry(session: Session) -> None:
    """One webhook episode creates its Series > Season > Episode chain, linked."""
    instance = _seed_instance(session)

    episode = upsert_series_episode_node(
        session,
        instance_id=instance.id,
        series_id=1,
        series_title="Breaking Bad",
        season_number=1,
        episode_id=101,
        episode_number=1,
        episode_title="Pilot",
    )

    assert episode.kind is LibraryNodeKind.EPISODE
    assert episode.sonarr_episode_id == 101
    assert episode.has_file is True
    series = _series(session, instance.id)
    season = _season(session, instance.id, 1)
    assert episode.parent_id == season.id
    assert season.parent_id == series.id
    assert series.parent_id is None
    # Exactly the three nodes for this one episode -- no siblings invented.
    assert len(list_nodes(session, instance.id)) == 3


def test_upsert_series_episode_node_is_idempotent_and_preserves_override(
    session: Session,
) -> None:
    """A repeat import updates in place and never clobbers a Tracked override."""
    instance = _seed_instance(session)
    upsert_series_episode_node(
        session,
        instance_id=instance.id,
        series_id=1,
        series_title="Breaking Bad",
        season_number=1,
        episode_id=101,
        episode_number=1,
        episode_title="Pilot",
    )
    # A user opts the series out of Tracked.
    series = _series(session, instance.id)
    set_tracked(session, node_id=series.id, tracked=False)

    # A later upgrade webhook for the same episode re-upserts the chain.
    upsert_series_episode_node(
        session,
        instance_id=instance.id,
        series_id=1,
        series_title="Breaking Bad",
        season_number=1,
        episode_id=101,
        episode_number=1,
        episode_title="Pilot (Remastered)",
    )

    assert len(list_nodes(session, instance.id)) == 3  # no duplicate rows
    episode = _episode(session, instance.id, 101)
    assert episode.title == "Pilot (Remastered)"  # updated in place
    # The explicit Not-Tracked override the user set survives the re-upsert and
    # the episode still resolves to it (cascade set it on the episode too).
    nodes_by_id = {n.id: n for n in list_nodes(session, instance.id)}
    default_tracked = get_global_settings(session).default_tracked
    assert resolve_tracked(episode, nodes_by_id, default_tracked) is False


def test_upsert_series_episode_node_un_hides_a_reappearing_node(session: Session) -> None:
    """A soft-hidden node re-imported via webhook is un-hidden, override intact."""
    instance = _seed_instance(session)
    upsert_series_episode_node(
        session,
        instance_id=instance.id,
        series_id=1,
        series_title="Breaking Bad",
        season_number=1,
        episode_id=101,
        episode_number=1,
        episode_title="Pilot",
    )
    # A later scan with an empty catalog soft-hides the whole tree.
    sync_library(session, instance_id=instance.id, catalog=SonarrCatalog(instance.id, series=()))
    assert all(n.hidden for n in list_nodes(session, instance.id))

    # A fresh import re-upserts and un-hides.
    episode = upsert_series_episode_node(
        session,
        instance_id=instance.id,
        series_id=1,
        series_title="Breaking Bad",
        season_number=1,
        episode_id=101,
        episode_number=1,
        episode_title="Pilot",
    )

    assert episode.hidden is False
    assert _series(session, instance.id).hidden is False


def test_upsert_movie_node_creates_a_flat_movie(session: Session) -> None:
    instance = _seed_instance(session, type_=InstanceType.RADARR)

    movie = upsert_movie_node(
        session, instance_id=instance.id, movie_id=1, title="Interstellar"
    )

    assert movie.kind is LibraryNodeKind.MOVIE
    assert movie.parent_id is None
    assert movie.radarr_movie_id == 1
    assert movie.has_file is True
    assert (
        get_node_by_source_id(session, instance_id=instance.id, radarr_movie_id=1) is not None
    )


def test_upsert_movie_node_is_idempotent_and_preserves_override(session: Session) -> None:
    instance = _seed_instance(session, type_=InstanceType.RADARR)
    movie = upsert_movie_node(session, instance_id=instance.id, movie_id=1, title="Interstellar")
    set_tracked(session, node_id=movie.id, tracked=False)

    upsert_movie_node(session, instance_id=instance.id, movie_id=1, title="Interstellar (2014)")

    nodes = list_nodes(session, instance.id)
    assert len(nodes) == 1
    assert nodes[0].title == "Interstellar (2014)"
    assert nodes[0].tracked_override is False
