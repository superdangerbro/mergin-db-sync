"""
Mergin DB Sync - a tool for two-way synchronization between Mergin and a PostGIS database

Copyright (C) 2020 Lutra Consulting

License: MIT
"""

import configparser
import json
import os
import shutil
import subprocess
import sys
import tempfile

import psycopg2

from mergin import MerginClient, MerginProject, LoginError


# set high logging level for geodiff (used by geodiffinfo executable)
# so we get as much information as possible
os.environ["GEODIFF_LOGGER_LEVEL"] = '4'   # 0 = nothing, 1 = errors, 2 = warning, 3 = info, 4 = debug


class DbSyncError(Exception):
    pass


class Config:
    """ Contains configuration of the sync """

    def __init__(self):
        self.project_working_dir = None
        self.geodiffinfo_exe = None

        self.mergin_url = 'https://public.cloudmergin.com'

        self.mergin_username = None
        self.mergin_password = None
        self.mergin_sync_file = None

        self.db_driver = None
        self.db_conn_info = None
        self.db_schema_modified = None
        self.db_schema_base = None

    def load(self, filename):
        cfg = configparser.ConfigParser()
        cfg.read(filename)

        self.project_working_dir = cfg['general']['working_dir']
        self.geodiffinfo_exe = cfg['general']['geodiffinfo_exe']

        self.mergin_username = cfg['mergin']['username']
        self.mergin_password = cfg['mergin']['password']
        self.mergin_sync_file = cfg['mergin']['sync_file']

        self.db_driver = cfg['db']['driver']
        self.db_conn_info = cfg['db']['conn_info']
        self.db_schema_modified = cfg['db']['modified']   # where local editing happens
        self.db_schema_base = cfg['db']['base']           # where only this script does changes


config = Config()


def _check_config():
    """ Makes sure that the configuration is valid, raises exceptions if not """
    if config.db_driver != "postgres":
        raise DbSyncError("Only 'postgres' driver is currently supported")


def _check_has_working_dir():
    if not os.path.exists(config.project_working_dir):
        raise DbSyncError("The project working directory does not exist: " + config.project_working_dir)

    if not os.path.exists(os.path.join(config.project_working_dir, '.mergin')):
        raise DbSyncError("The project working directory does not seem to contain Mergin project: " + config.project_working_dir)


def _check_has_sync_file():
    """ Checks whether the dbsync environment is initialized already (so that we can pull/push).
     Emits an exception if not initialized yet. """

    gpkg_full_path = os.path.join(config.project_working_dir, config.mergin_sync_file)
    if not os.path.exists(gpkg_full_path):
        raise DbSyncError("The output GPKG file does not exist: " + gpkg_full_path)


def _check_schema_exists(conn, schema_name):
    cur = conn.cursor()
    cur.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = %s)", (schema_name,))
    return cur.fetchone()[0]


def _run_geodiff(cmd):
    """ will run a command (with geodiffinfo) and report what got to stderr and raise exception
    if the command returns non-zero exit code """
    res = subprocess.run(cmd, stderr=subprocess.PIPE)
    geodiff_stderr = res.stderr.decode()
    if geodiff_stderr:
        print("GEODIFF: " + geodiff_stderr)
    if res.returncode != 0:
        raise DbSyncError("geodiffinfo failed!\n" + str(cmd))


def _geodiff_create_changeset(driver, conn_info, base, modified, changeset):
    _run_geodiff([config.geodiffinfo_exe, "createChangesetEx", driver, conn_info, base, modified, changeset])


def _geodiff_apply_changeset(driver, conn_info, base, changeset):
    _run_geodiff([config.geodiffinfo_exe, "applyChangesetEx", driver, conn_info, base, changeset])


def _geodiff_list_changes_summary(changeset):
    """ Returns a list with changeset summary:
     [ { 'table': 'foo', 'insert': 1, 'update': 2, 'delete': 3 }, ... ]
    """
    tmp_dir = tempfile.gettempdir()
    tmp_output = os.path.join(tmp_dir, 'dbsync-changeset-summary')
    if os.path.exists(tmp_output):
        os.remove(tmp_output)
    _run_geodiff([config.geodiffinfo_exe, "listChangesSummary", changeset, tmp_output])
    with open(tmp_output) as f:
        out = json.load(f)
    os.remove(tmp_output)
    return out["geodiff_summary"]


def _geodiff_make_copy(src_driver, src_conn_info, src, dst_driver, dst_conn_info, dst):
    _run_geodiff([config.geodiffinfo_exe, "makeCopy", src_driver, src_conn_info, src, dst_driver, dst_conn_info, dst])


def _print_changes_summary(summary):
    print("Changes:")
    for item in summary:
        print("{:20} {:4} {:4} {:4}".format(item['table'], item['insert'], item['update'], item['delete']))


def _get_project_version():
    """ Returns the current version of the project """
    mp = MerginProject(config.project_working_dir)
    return mp.metadata["version"]


def dbsync_pull():
    """ Downloads any changes from Mergin and applies them to the database """

    _check_has_working_dir()
    _check_has_sync_file()

    mc = MerginClient(config.mergin_url, login=config.mergin_username, password=config.mergin_password)

    status_pull, status_push, _ = mc.project_status(config.project_working_dir)
    if not status_pull['added'] and not status_pull['updated'] and not status_pull['removed']:
        print("No changes on Mergin.")
        return
    if status_push['added'] or status_push['updated'] or status_push['removed']:
        raise DbSyncError("There are pending changes in the local directory - that should never happen! " + str(status_push))

    gpkg_basefile = os.path.join(config.project_working_dir, '.mergin', config.mergin_sync_file)
    gpkg_basefile_old = gpkg_basefile + "-old"

    # make a copy of the basefile in the current version (base) - because after pull it will be set to "their"
    shutil.copy(gpkg_basefile, gpkg_basefile_old)

    tmp_dir = tempfile.gettempdir()
    tmp_base2our = os.path.join(tmp_dir, 'dbsync-pull-base2our')
    tmp_base2their = os.path.join(tmp_dir, 'dbsync-pull-base2their')

    # find out our local changes in the database (base2our)
    _geodiff_create_changeset(config.db_driver, config.db_conn_info, config.db_schema_base, config.db_schema_modified, tmp_base2our)

    if os.path.getsize(tmp_base2our) != 0:
        raise DbSyncError("Rebase not supported yet!")

    # TODO: when rebasing: apply local DB changes to gpkg  (base2our)

    mc.pull_project(config.project_working_dir)  # will do rebase as needed

    print("Pulled new version from Mergin: " + _get_project_version())

    # simple case when there are no pending local changes - just apply whatever changes are coming
    _geodiff_create_changeset("sqlite", "", gpkg_basefile_old, gpkg_basefile, tmp_base2their)

    # summarize changes
    summary = _geodiff_list_changes_summary(tmp_base2their)
    _print_changes_summary(summary)

    _geodiff_apply_changeset(config.db_driver, config.db_conn_info, config.db_schema_base, tmp_base2their)
    _geodiff_apply_changeset(config.db_driver, config.db_conn_info, config.db_schema_modified, tmp_base2their)

    # TODO: when rebasing:
    # - createChangesetEx - using gpkg (their2our)
    # - applyChangesetEx - using DB modified (inv base2our + base2their + their2our)
    # - applyChangesetEx - using DB base (base2their)

    os.remove(gpkg_basefile_old)


def dbsync_status():
    """ Figure out if there are any pending changes in the database or in Mergin """

    _check_has_working_dir()
    _check_has_sync_file()

    # print basic information
    mp = MerginProject(config.project_working_dir)
    project_path = mp.metadata["name"]
    local_version = mp.metadata["version"]
    print("Working directory " + config.project_working_dir)
    print("Mergin project " + project_path + " at local version " + local_version)
    print("")
    print("Checking status...")

    mc = MerginClient(config.mergin_url, login=config.mergin_username, password=config.mergin_password)

    # check if there are any pending changes on server
    gpkg_full_path = os.path.join(config.project_working_dir, config.mergin_sync_file)
    status_pull, status_push, _ = mc.project_status(config.project_working_dir)
    if status_pull['added'] or status_pull['updated'] or status_pull['removed']:
        print("There are pending changes on server: " + str(status_pull))
    else:
        print("No pending changes on server.")

    if status_push['added'] or status_push['updated'] or status_push['removed']:
        raise DbSyncError("There are pending changes in the local directory - that should never happen! " + str(status_push))

    print("")
    conn = psycopg2.connect(config.db_conn_info)

    if not _check_schema_exists(conn, config.db_schema_base):
        raise DbSyncError("The base schema does not exist: " + config.db_schema_base)
    if not _check_schema_exists(conn, config.db_schema_modified):
        raise DbSyncError("The 'modified' schema does not exist: " + config.db_schema_modified)

    # get changes in the DB
    tmp_dir = tempfile.gettempdir()
    tmp_changeset_file = os.path.join(tmp_dir, 'dbsync-status-base2our')
    if os.path.exists(tmp_changeset_file):
        os.remove(tmp_changeset_file)
    _geodiff_create_changeset(config.db_driver, config.db_conn_info, config.db_schema_base, config.db_schema_modified, tmp_changeset_file)

    if os.path.getsize(tmp_changeset_file) == 0:
        print("No changes in the database.")
    else:
        print("There are changes in DB")
        # summarize changes
        summary = _geodiff_list_changes_summary(tmp_changeset_file)
        _print_changes_summary(summary)


def dbsync_push():
    """ Take changes in the 'modified' schema in the database and push them to Mergin """

    tmp_dir = tempfile.gettempdir()
    tmp_changeset_file = os.path.join(tmp_dir, 'dbsync-push-base2our')
    if os.path.exists(tmp_changeset_file):
        os.remove(tmp_changeset_file)

    _check_has_working_dir()
    _check_has_sync_file()

    mc = MerginClient(config.mergin_url, login=config.mergin_username, password=config.mergin_password)

    # check there are no pending changes on server (or locally - which should never happen)
    gpkg_full_path = os.path.join(config.project_working_dir, config.mergin_sync_file)
    status_pull, status_push, _ = mc.project_status(config.project_working_dir)
    if status_pull['added'] or status_pull['updated'] or status_pull['removed']:
        raise DbSyncError("There are pending changes on server - need to pull them first: " + str(status_pull))
    if status_push['added'] or status_push['updated'] or status_push['removed']:
        raise DbSyncError("There are pending changes in the local directory - that should never happen! " + str(status_push))

    conn = psycopg2.connect(config.db_conn_info)

    if not _check_schema_exists(conn, config.db_schema_base):
        raise DbSyncError("The base schema does not exist: " + config.db_schema_base)
    if not _check_schema_exists(conn, config.db_schema_modified):
        raise DbSyncError("The 'modified' schema does not exist: " + config.db_schema_modified)

    # get changes in the DB
    _geodiff_create_changeset(config.db_driver, config.db_conn_info, config.db_schema_base, config.db_schema_modified, tmp_changeset_file)

    if os.path.getsize(tmp_changeset_file) == 0:
        print("No changes in the database.")
        return

    # summarize changes
    summary = _geodiff_list_changes_summary(tmp_changeset_file)
    _print_changes_summary(summary)

    # write changes to the local geopackage
    _geodiff_apply_changeset("sqlite", "", gpkg_full_path, tmp_changeset_file)

    # write to the server
    mc.push_project(config.project_working_dir)

    print("Pushed new version to Mergin: " + _get_project_version())

    # update base schema in the DB
    _geodiff_apply_changeset(config.db_driver, config.db_conn_info, config.db_schema_base, tmp_changeset_file)


def dbsync_init():
    """ Initialize the dbsync so that it is possible to do two-way sync between Mergin and a database """

    _check_has_working_dir()

    gpkg_full_path = os.path.join(config.project_working_dir, config.mergin_sync_file)
    if os.path.exists(gpkg_full_path):
        raise DbSyncError("The output GPKG file exists already: " + gpkg_full_path)

    try:
        mc = MerginClient(config.mergin_url, login=config.mergin_username, password=config.mergin_password)
    except LoginError:
        raise DbSyncError("Unable to log in to Mergin: have you specified correct credentials in configuration file?")

    # check there are no pending changes on server (or locally - which should never happen)
    status_pull, status_push, _ = mc.project_status(config.project_working_dir)
    if status_pull['added'] or status_pull['updated'] or status_pull['removed']:
        raise DbSyncError("There are pending changes on server - need to pull them first: " + str(status_pull))
    if status_push['added'] or status_push['updated'] or status_push['removed']:
        raise DbSyncError("There are pending changes in the local directory - that should never happen! " + str(status_push))

    try:
        conn = psycopg2.connect(config.db_conn_info)
    except psycopg2.Error as e:
        raise DbSyncError("Unable to connect to the database: " + str(e))

    if _check_schema_exists(conn, config.db_schema_base):
        raise DbSyncError("The base schema already exists: " + config.db_schema_base)

    if not _check_schema_exists(conn, config.db_schema_modified):
        raise DbSyncError("The 'modified' schema does not exist: " + config.db_schema_modified)

    # COPY: modified -> base
    _geodiff_make_copy(config.db_driver, config.db_conn_info, config.db_schema_modified,
                       config.db_driver, config.db_conn_info, config.db_schema_base)

    # COPY: modified -> gpkg
    _geodiff_make_copy(config.db_driver, config.db_conn_info, config.db_schema_modified,
                       "sqlite", "", gpkg_full_path)

    # upload gpkg to mergin (client takes care of storing metadata)
    mc.push_project(config.project_working_dir)


def show_usage():
    print("dbsync")
    print("")
    print("    dbsync init        = will create base schema in DB + create gpkg file in working copy")
    print("    dbsync status      = will check whether there is anything to pull or push")
    print("    dbsync push        = will push changes from DB to mergin")
    print("    dbsync pull        = will pull changes from mergin to DB")


def load_config(config_filename):
    if not os.path.exists(config_filename):
        raise DbSyncError("The configuration file does not exist: " + config_filename)
    config.load(config_filename)
    _check_config()


def main():
    if len(sys.argv) < 2:
        show_usage()
        return

    config_filename = 'config.ini'

    try:
        load_config(config_filename)

        if sys.argv[1] == 'init':
            print("Initializing...")
            dbsync_init()
        elif sys.argv[1] == 'status':
            dbsync_status()
        elif sys.argv[1] == 'push':
            print("Pushing...")
            dbsync_push()
        elif sys.argv[1] == 'pull':
            print("Pulling...")
            dbsync_pull()
        else:
            show_usage()
    except DbSyncError as e:
        print("Error: " + str(e))


if __name__ == '__main__':
    main()
