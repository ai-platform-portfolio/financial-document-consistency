// Local validation only; deployment uses chart-rendered Atlas resources.
env "local" {
  url = getenv("DATABASE_URL")
  dev = getenv("DEV_DATABASE_URL")

  migration {
    dir = "file://migrations"
    revisions_schema = "atlas"
  }

  test {
    migrate {
      src = ["tests/migrations.test.hcl"]
    }
  }
}
