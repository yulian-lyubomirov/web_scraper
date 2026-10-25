CREATE TABLE IF NOT EXISTS cities (
    name TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS neighborhoods (
    city_name TEXT NOT NULL REFERENCES cities(name),
    name TEXT NOT NULL,
    PRIMARY KEY (city_name, name)
);

CREATE TABLE IF NOT EXISTS assets (
    id SERIAL PRIMARY KEY,
    city_name TEXT NOT NULL,
    neighborhood_name TEXT,
    FOREIGN KEY (city_name, neighborhood_name)
        REFERENCES neighborhoods(city_name, name),
    source TEXT NOT NULL,
    source_listing_id TEXT NOT NULL,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    price NUMERIC(14, 2) NOT NULL,
    size NUMERIC(12, 2) NOT NULL,
    num_bedrooms INTEGER,
    metadata JSONB,
    url TEXT,
    scraped_at TIMESTAMPTZ,
    UNIQUE (source, source_listing_id)
);
