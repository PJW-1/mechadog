// Compile-only values, not credentials or an installable TLS configuration.
// CI builds stay outside release artifact paths. Never flash this configuration.
#define MECHADOG_ENABLE_OTA 1
#define MECHADOG_ENABLE_ACTUATORS 0
#define MECHADOG_ENABLE_SENSORS 1
#define MECHADOG_ENABLE_TASK_WDT 1
#define MECHADOG_OTA_TOKEN "compile-only-not-a-credential"
#define MECHADOG_OTA_CERT "compile-only-not-a-certificate"
#define MECHADOG_OTA_KEY "compile-only-not-a-key"
