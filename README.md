# MindsDB on Platform.sh

1. requires pip to be updated
2. requires larger build container (tested with 6GB but much lower should work)
3. tested with a large plan, works on small .. but seems to go out of memory early

TODO:
1. Assets should not be served by the app, and should be cached.
2. API endpoint should not be hard-coded to 127.0.0.1 (editing manually the generate js works)
3. There is no good reason to use waitress
4. Discuss usage of psycog2-binary - not a good practice
