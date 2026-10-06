class Error998(Exception):
    ...


class RequiresCapcha(Exception):
    ...


class JpDownloadIncomplete(Exception):
    """JP book download finished without full verified spread coverage."""

    def __init__(
        self,
        title: str,
        verified: int,
        total_spreads: int,
        failed_spreads: list[int],
        suspect_pairs: list[tuple[int, int]],
    ):
        self.title = title
        self.verified = verified
        self.total_spreads = total_spreads
        self.failed_spreads = failed_spreads
        self.suspect_pairs = suspect_pairs
        missing = total_spreads - verified
        super().__init__(
            f"{title}: {verified}/{total_spreads} spreads verified "
            f"({missing} incomplete, {len(suspect_pairs)} suspect duplicate(s))"
        )
