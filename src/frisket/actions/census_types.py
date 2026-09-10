"""Typed Census demographic values shared by actions and the host capability."""

from pydantic import BaseModel


class CensusRecord(BaseModel):
    demo_population: float | None = None
    demo_median_age: float | None = None
    demo_median_household_income: float | None = None
    demo_poverty_rate: float | None = None
    demo_bachelors_plus_rate: float | None = None
    demo_white_non_hispanic_pct: float | None = None
    demo_black_pct: float | None = None
    demo_hispanic_pct: float | None = None
    demo_asian_pct: float | None = None
    demo_owner_occupied_pct: float | None = None
    demo_provider: str | None = None
    demo_dataset: str | None = None
    demo_vintage: str | None = None
    demo_area_level: str | None = None
    demo_area_id: str | None = None
    us_census_state_fips: str | None = None
    us_census_county_fips: str | None = None
    us_census_tract_geoid: str | None = None
    us_census_block_group_geoid: str | None = None
    demo_population_moe: float | None = None
    demo_median_age_moe: float | None = None
    demo_median_household_income_moe: float | None = None
