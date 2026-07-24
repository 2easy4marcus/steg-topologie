# Intentionally empty: this file used to hold a throwaway test written
# during early scaffolding to manually trigger the isolated_db fixture.
# That fixture is declared autouse=True in conftest.py, so it already runs
# for every test in the suite -- this file added no coverage of its own.
# Left as an empty module (rather than deleted) because this sandbox's
# mounted filesystem doesn't permit removing existing files; delete it
# freely on a normal filesystem.
