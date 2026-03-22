# BOSS Search Elements

## Search Strategy
The search script does not rely on manual UI filters.
It uses this sequence:
1. open BOSS jobs homepage
2. fill keyword in the search input
3. press `Enter`
4. open the fixed filtered search URL
5. collect cards from the result DOM
6. paginate with `&page=N`

## Search Input Selectors
These selectors are used to detect or fill the search box:
- `input[placeholder*='搜索']`
- `input.ipt-search`
- `input[ka='header-home-search']`
- `input[name='query']`

## Result Container Selectors
These selectors indicate the script is on the result page:
- `.job-list-box`
- `.search-job-result`
- `.job-list`
- `.job-list-container`

## Job Card Selectors
These selectors are used to enumerate cards:
- `.job-list-box .job-card-wrapper`
- `.search-job-result .job-card-box`
- `.job-list li`
- `.job-card-box`

## Card Field Roles
- Title: `.job-name`
- Company: `.boss-name`, `.company-name`
- Salary: `.job-salary`
- Tags: `.tag-list li`
- Location: `.company-location`
- Link: any `a[href*='job_detail']`

## Pagination Rule
The script does not click the pagination UI.
It rebuilds the search URL and appends `page=N`.

## Verification / Risk Signals
The search script treats these as verification signals:
- iframe url/id/class contains `captcha`, `verify`, `challenge`, `security`
- DOM contains `[class*='verify']`, `[id*='verify']`, `[class*='captcha']`, `[id*='captcha']`
- current URL contains `captcha`, `verify`, `challenge`, `security`

## Why These Elements Matter
- Search input verifies that the keyword really entered the SPA state.
- Result container proves the script is on the list page, not homepage fallback.
- Card selectors let the script extract data without clicking into every item.
- Verification selectors decide whether to pause for manual intervention.
