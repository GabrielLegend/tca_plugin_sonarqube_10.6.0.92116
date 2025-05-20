set -x

export CURRENT=$(cd "$(dirname "$0")";pwd)
export SOURCE_DIR="${CURRENT}/source"
export RESULT_DIR="${CURRENT}/workdir/out"
export TASK_REQUEST="${CURRENT}/task_request.json"

echo $SOURCE_DIR
echo $RESULT_DIR

echo "- * - * - * - * - * - * - * - * - * - * - * - * -* -* -* -* -* -* -"
python3 ${CURRENT}/../src/check.py

echo "- * - * - * - * - * - * - * - * - * - * - * - * -* -* -* -* -* -* -"
python3 ${CURRENT}/../src/sq.py scan 2>&1 | tee ${CURRENT}/run.log
echo "- * - * - * - * - * - * - * - * - * - * - * - * -* -* -* -* -* -* -"


# works
# docker run -it --rm -v /data/yale/git/tca_plugin_sonarqube_10.6.0.92116:/data tencentos/tencentos_server31:latest bash


